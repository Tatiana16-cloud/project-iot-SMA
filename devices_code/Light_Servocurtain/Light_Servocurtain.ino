#include <WiFi.h>
#include <PubSubClient.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
#include <ESP32Servo.h>
#include <ArduinoJson.h>

// ============================================================================
// CONFIGURATION
// ============================================================================

// ---- Wi-Fi & MQTT ----
#define WIFI_SSID               "Wokwi-GUEST"
#define WIFI_PASS               ""
#define MQTT_BROKER_HOST        "broker.hivemq.com"
#define MQTT_BROKER_PORT        1883
#define MQTT_BUFFER_SIZE        2048
#define MQTT_RECONNECT_DELAY_MS 2000

// ---- Catalog ----
// Paste the minted deviceID from the catalog (dev-<hex>) after registering a room.
// The device uses this ID to look itself up in /rooms and discover its user/room.
#define DEVICE_ID               "dev-xxxxxxxxxx"
#define CATALOG_BASE_URL        "https://moody-tables-sits-brian.trycloudflare.com" // Update with your tunnel URL
#define CATALOG_WRITE_TOKEN     ""

// ---- Flags ----
#define USE_WIFI                1
#define USE_CATALOG_LOOKUP      1
#define USE_MQTT                1

// ---- Hardware Pins ----
#define PIN_LED                 4
#define PIN_SERVO               15
#define PIN_POT                 34  // Photoresistor/Potentiometer

// ---- Timing ----
#define TELEMETRY_INTERVAL_MS   1000
#define WIFI_CONNECT_TIMEOUT_MS 10000
#define HTTP_TIMEOUT_MS         10000

// ============================================================================
// GLOBALS
// ============================================================================

WiFiClient   wifiClient;
PubSubClient mqttClient(wifiClient);
Servo        curtainServo;

// Identity (resolved at runtime from catalog by DEVICE_ID lookup)
String userId = "";
String roomId = "";

// Dynamic MQTT Topics
String topicLightPub;
String topicServoCurtainState;
String topicServoCurtainCmd;
String topicLedState;
String topicLedCmd;
String topicSampling;
String topicDown;

// State
int  servoAngle = 0;       // 0=closed, 90=open
bool ledState   = false;
bool samplingEnabled = false; 
unsigned long lastTelemetryMs = 0;

// ============================================================================
// NETWORK HELPERS (Identical to Sensors)
// ============================================================================

bool httpGet(const String& url, String& responseBody) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[http] Error: WiFi not connected");
    return false;
  }

  HTTPClient http;
  WiFiClientSecure *secureClient = nullptr;

  if (url.startsWith("https://")) {
    secureClient = new WiFiClientSecure;
    if (secureClient) {
      secureClient->setInsecure();
      secureClient->setTimeout(HTTP_TIMEOUT_MS);
      http.begin(*secureClient, url);
    } else {
      Serial.println("[http] Failed to init WiFiClientSecure");
      return false;
    }
  } else {
    http.begin(url);
  }

  http.setConnectTimeout(HTTP_TIMEOUT_MS);
  
  Serial.printf("[http] GET %s\n", url.c_str());
  int code = http.GET();
  
  bool success = (code >= 200 && code < 300);
  if (success) {
    responseBody = http.getString();
    Serial.printf("[http] Success: %d (len=%d)\n", code, responseBody.length());
  } else {
    Serial.printf("[http] Failed: %d (%s)\n", code, http.errorToString(code).c_str());
  }

  http.end();
  if (secureClient) delete secureClient;
  return success;
}

bool httpPatch(const String& url, const String& jsonPayload, String& responseBody) {
  if (WiFi.status() != WL_CONNECTED) return false;

  HTTPClient http;
  WiFiClientSecure *secureClient = nullptr;

  if (url.startsWith("https://")) {
    secureClient = new WiFiClientSecure;
    if (secureClient) {
      secureClient->setInsecure();
      secureClient->setTimeout(HTTP_TIMEOUT_MS);
      http.begin(*secureClient, url);
    } else {
      Serial.println("[http] Failed to init WiFiClientSecure");
      return false;
    }
  } else {
    http.begin(url);
  }

  http.addHeader("Content-Type", "application/json");

  if (strlen(CATALOG_WRITE_TOKEN) > 0) {
    http.addHeader("X-Write-Token", CATALOG_WRITE_TOKEN);
  }

  Serial.printf("[http] PATCH %s\n", url.c_str());
  int code = http.PATCH((uint8_t*)jsonPayload.c_str(), jsonPayload.length());

  bool success = (code >= 200 && code < 300);
  if (success) {
    responseBody = http.getString();
    Serial.printf("[http] Success: %d\n", code);
  } else {
    Serial.printf("[http] Failed: %d (%s)\n", code, http.errorToString(code).c_str());
  }

  http.end();
  if (secureClient) delete secureClient;
  return success;
}

// ============================================================================
// CATALOG INTEGRATION
// ============================================================================

bool resolveIdentityFromCatalog() {
  String response;
  String url = String(CATALOG_BASE_URL) + "/rooms";
  
  if (!httpGet(url, response)) {
    Serial.println("[catalog] Failed to fetch rooms");
    return false;
  }

  StaticJsonDocument<4096> doc;
  DeserializationError err = deserializeJson(doc, response);
  if (err) {
    Serial.printf("[catalog] JSON parse error: %s\n", err.c_str());
    return false;
  }
  if (!doc.is<JsonArray>()) {
    Serial.println("[catalog] Error: /rooms did not return an array");
    return false;
  }

  for (JsonObject room : doc.as<JsonArray>()) {
    const char* rUser = room["userID"] | "";
    const char* rRoom = room["roomID"] | "";

    JsonArray devices = room["connected_devices"].as<JsonArray>();
    for (JsonObject dev : devices) {
      const char* did = dev["deviceID"] | "";
      if (strcmp(did, DEVICE_ID) == 0) {
        userId = String(rUser);
        roomId = String(rRoom);
        Serial.printf("[catalog] Identity resolved: Device=%s User=%s Room=%s\n",
                      DEVICE_ID, userId.c_str(), roomId.c_str());
        return (!roomId.isEmpty() && !userId.isEmpty());
      }
    }
  }

  Serial.printf("[catalog] Device '%s' not found in any room\n", DEVICE_ID);
  return false;
}

bool updateDeviceInCatalog() {
  if (roomId.isEmpty() || userId.isEmpty()) return false;

  StaticJsonDocument<1024> doc;
  JsonArray availableServices = doc.createNestedArray("availableServices");
  availableServices.add("MQTT");

  JsonArray servicesDetails = doc.createNestedArray("servicesDetails");
  JsonObject svc = servicesDetails.createNestedObject();
  svc["serviceType"] = "MQTT";

  JsonArray pubTopics = svc.createNestedArray("topic_pub");
  pubTopics.add(topicServoCurtainState);
  pubTopics.add(topicLightPub);
  pubTopics.add(topicLedState);
  pubTopics.add(topicDown);

  JsonArray subTopics = svc.createNestedArray("topic_sub");
  subTopics.add(topicServoCurtainCmd);
  subTopics.add(topicLedCmd);
  subTopics.add(topicSampling);

  doc["timestamp"] = "device-local-ts"; 

  String payload;
  serializeJson(doc, payload);

  String response;
  String url = String(CATALOG_BASE_URL) + "/devices/" + DEVICE_ID;
  return httpPatch(url, payload, response);
}

void constructTopics() {
  topicServoCurtainState = "SC/" + userId + "/" + roomId + "/servoCurtain";
  topicServoCurtainCmd   = topicServoCurtainState + "/set";
  topicLightPub     = "SC/" + userId + "/" + roomId + "/Light";
  topicLedState     = "SC/" + userId + "/" + roomId + "/LedL";
  topicLedCmd       = topicLedState + "/set";
  topicSampling     = "SC/" + userId + "/" + roomId + "/sampling";
  topicDown         = "SC/" + userId + "/" + roomId + "/down";
}

// ============================================================================
// WIFI & MQTT (Identical to Sensors)
// ============================================================================

void connectWiFi() {
  if (!USE_WIFI) return;
  if (WiFi.status() == WL_CONNECTED) return;

  Serial.printf("[wifi] Connecting to %s", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
    if (millis() - start > WIFI_CONNECT_TIMEOUT_MS) {
      Serial.println("\n[wifi] Timeout!");
      break;
    }
  }
  Serial.println();
  
  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("[wifi] Connected. IP: ");
    Serial.println(WiFi.localIP());
  }
}

void connectMQTT() {
  if (!USE_MQTT) return;
  if (mqttClient.connected()) return;

  mqttClient.setServer(MQTT_BROKER_HOST, MQTT_BROKER_PORT);
  mqttClient.setBufferSize(MQTT_BUFFER_SIZE);

  while (!mqttClient.connected()) {
    String clientId = "sc-dev-" + String(DEVICE_ID);
    Serial.printf("[mqtt] Connecting as %s...\n", clientId.c_str());

    if (mqttClient.connect(clientId.c_str())) {
      Serial.println("[mqtt] Connected!");
      
      mqttClient.subscribe(topicServoCurtainCmd.c_str(), 1);
      mqttClient.subscribe(topicLedCmd.c_str(), 1);
      mqttClient.subscribe(topicSampling.c_str(), 1);
      
      Serial.printf("[mqtt] Subscribed: %s\n", topicServoCurtainCmd.c_str());
      Serial.printf("[mqtt] Subscribed: %s\n", topicLedCmd.c_str());
      Serial.printf("[mqtt] Subscribed: %s\n", topicSampling.c_str());
    } else {
      Serial.printf("[mqtt] Failed, rc=%d. Retry in %dms\n", mqttClient.state(), MQTT_RECONNECT_DELAY_MS);
      delay(MQTT_RECONNECT_DELAY_MS);
    }
  }
}

// ============================================================================
// APPLICATION LOGIC
// ============================================================================

void publishLedState() {
  if (!USE_MQTT) return;
  String payload = "[{\"bn\":\"stateLed\",\"bt\":0,\"e\":[{\"n\":\"LedL\",\"u\":\"bool\",\"vb\":";
  payload += (ledState ? "true" : "false");
  payload += "}]}]";
  mqttClient.publish(topicLedState.c_str(), payload.c_str());
  Serial.println("[mqtt] PUB LED: " + payload);
}

void publishServoState() {
  if (!USE_MQTT) return;
  bool isOpen = (servoAngle == 90);
  String payload = "[{\"bn\":\"ServoState\",\"bt\":0,\"e\":[{\"n\":\"servoCurtain\",\"u\":\"bool\",\"vb\":";
  payload += (isOpen ? "true" : "false");
  payload += "}]}]";
  mqttClient.publish(topicServoCurtainState.c_str(), payload.c_str());
  Serial.println("[mqtt] PUB Curtain: " + payload);
}

void publishLightValue(int rawVal) {
  if (!USE_MQTT) return;
  char payload[128];
  snprintf(payload, sizeof(payload), 
    "[{\"bn\":\"lightValue\",\"bt\":0,\"e\":[{\"n\":\"raw\",\"u\":\"lm\",\"v\":%d}]}]", rawVal);
    
  mqttClient.publish(topicLightPub.c_str(), payload);
  Serial.println("[mqtt] PUB Light: " + String(payload));
}

void publishDeviceStatus(const char* statusText) {
  if (!USE_MQTT) return;
  char buf[256];
  snprintf(buf, sizeof(buf),
           "{\"device\":\"%s\",\"type\":\"actuator\",\"status\":\"%s\",\"servoCurtain\":%s,\"led\":%s,\"sampling\":%s}",
           DEVICE_ID,
           statusText,
           (servoAngle == 90) ? "true" : "false",
           ledState ? "true" : "false", 
           samplingEnabled ? "true" : "false");
           
  mqttClient.publish(topicDown.c_str(), buf);
  Serial.printf("[mqtt] PUB Status: %s\n", buf);
}

void handleLedCommand(const char* payload) {
  StaticJsonDocument<512> doc;
  DeserializationError err = deserializeJson(doc, payload);
  bool newState = ledState;
  bool commandFound = false;

  if (!err && doc.is<JsonArray>()) {
    JsonArray arr = doc.as<JsonArray>();
    for (JsonObject rec : arr) {
      JsonArray events = rec["e"];
      for (JsonObject e : events) {
        const char* n = e["n"] | "";
        if (strcmp(n, "LedL") == 0) {
          if (e.containsKey("vb")) { newState = e["vb"]; commandFound = true; }
          else if (e.containsKey("v")) { newState = (e["v"] != 0); commandFound = true; }
        }
      }
    }
  }

  if (!commandFound) {
    String s = String(payload);
    s.trim();
    s.toUpperCase();
    if (s == "ON" || s == "1" || s == "TRUE") { newState = true; commandFound = true; }
    else if (s == "OFF" || s == "0" || s == "FALSE") { newState = false; commandFound = true; }
  }

  if (commandFound && newState != ledState) {
    ledState = newState;
    digitalWrite(PIN_LED, ledState ? HIGH : LOW);
    Serial.printf("[actuator] LED -> %s\n", ledState ? "ON" : "OFF");
    publishLedState();
  }
}

void handleServoCommand(const char* payload) {
  if (payload[0] == '[') return;

  int deg = String(payload).toInt();
  if (deg != 0 && deg != 90) {
    Serial.println("[actuator] Servo command ignored: only 0 or 90 allowed");
    return;
  }
  
  if (deg != servoAngle) {
    servoAngle = deg;
    curtainServo.write(servoAngle);
    Serial.printf("[actuator] Curtain -> %d\n", servoAngle);
    publishServoState();
  }
}

void handleSamplingMessage(const char* payload) {
  StaticJsonDocument<256> doc;
  DeserializationError err = deserializeJson(doc, payload);
  if (err) return;

  bool enable = doc["enable"] | false;
  if (enable != samplingEnabled) {
    samplingEnabled = enable;
    
    publishDeviceStatus(samplingEnabled ? "MONITORING_ON" : "MONITORING_OFF");
    Serial.printf("[control] Sampling -> %s\n", samplingEnabled ? "ENABLED" : "DISABLED");
  }
}

void mqttCallback(char* topic, byte* payload, unsigned int len) {
  if (!USE_MQTT) return;
  
  static char msgBuf[2048];
  size_t copyLen = (len < sizeof(msgBuf)) ? len : (sizeof(msgBuf) - 1);
  memcpy(msgBuf, payload, copyLen);
  msgBuf[copyLen] = '\0';

  Serial.printf("[mqtt] RX %s (%d bytes)\n", topic, len);

  if (topicLedCmd == topic) {
    handleLedCommand(msgBuf);
  } else if (topicServoCurtainCmd == topic) {
    handleServoCommand(msgBuf);
  } else if (topicSampling == topic) {
    handleSamplingMessage(msgBuf);
  }
}

// ============================================================================
// MAIN SETUP & LOOP
// ============================================================================

void setup() {
  Serial.begin(115200);
  Serial.println("\n\n--- IoT Actuator Device Booting ---");

  // Init Hardware
  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, LOW);
  
  curtainServo.attach(PIN_SERVO, 500, 2400);
  curtainServo.write(servoAngle);

  analogReadResolution(12);

  mqttClient.setCallback(mqttCallback);

  connectWiFi();

  #if USE_CATALOG_LOOKUP
    if (resolveIdentityFromCatalog()) {
      constructTopics();
      updateDeviceInCatalog();
    } else {
      Serial.println("[setup] Catalog resolution failed. Device will not publish until catalog is reachable.");
      return;
    }
  #else
    Serial.println("[setup] Catalog lookup disabled (flag off). Cannot publish without identity.");
    return;
  #endif

  if (USE_MQTT) {
    connectMQTT();
    publishLedState();
    publishServoState();
  }
}

void loop() {
  connectWiFi();
  
  if (USE_MQTT) {
    connectMQTT();
    mqttClient.loop();
  }

  if (!samplingEnabled) {
    delay(200);
    return;
  }

  unsigned long now = millis();
  if (now - lastTelemetryMs >= TELEMETRY_INTERVAL_MS) {
    lastTelemetryMs = now;
    int raw = analogRead(PIN_POT);
    publishLightValue(raw);
  }
}
