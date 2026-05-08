#include <WiFi.h>
#include <PubSubClient.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
#include <DHTesp.h>
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
// Paste here the dev-<hex> UUID that the catalog minted for this physical
// device (visible in GET /rooms -> connected_devices). The firmware will
// look it up in /rooms to discover which userID and roomID it belongs to.
#define DEVICE_ID               "dev-xxxxxxxxxx"
#define CATALOG_BASE_URL        "https://moody-tables-sits-brian.trycloudflare.com" // Update with your tunnel URL
#define CATALOG_WRITE_TOKEN     ""

// ---- Flags ----
#define USE_WIFI                1
#define USE_CATALOG_LOOKUP      1
#define USE_MQTT                1

// ---- Hardware Pins ----
#define PIN_DHT                 15
#define PIN_SERVO               18

// ---- Timing ----
#define TELEMETRY_INTERVAL_MS   2000
#define WIFI_CONNECT_TIMEOUT_MS 10000
#define HTTP_TIMEOUT_MS         10000

// ============================================================================
// GLOBALS
// ============================================================================

WiFiClient   wifiClient;
PubSubClient mqttClient(wifiClient);
DHTesp       dht;
Servo        fan;

// Identity (userId/roomId resolved at runtime from catalog)
String userId = "";
String roomId = "";

// Dynamic MQTT Topics
String topicUp;
String topicServo;
String topicAlertDhtExact;
String topicAlertDhtWildcard;
String topicDown;
String topicSampling;

// State
bool servoOn = false;
bool samplingEnabled = false; 
unsigned long lastTelemetryMs = 0;

// ============================================================================
// NETWORK HELPERS
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
    JsonArray devices = room["connected_devices"].as<JsonArray>();
    for (JsonObject dev : devices) {
      const char* did = dev["deviceID"] | "";
      if (strcmp(did, DEVICE_ID) == 0) {
        userId = String(room["userID"] | "");
        roomId = String(room["roomID"] | "");
        Serial.printf("[catalog] Identity resolved: Device=%s -> User=%s Room=%s\n",
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
  doc["availableServices"] = JsonArray();
  doc["availableServices"].add("MQTT");

  JsonObject svc = doc["servicesDetails"].createNestedObject();
  svc["serviceType"] = "MQTT";

  JsonArray pubTopics = svc.createNestedArray("topic_pub");
  pubTopics.add(topicUp);
  pubTopics.add(topicServo);
  pubTopics.add(topicDown);

  JsonArray subTopics = svc.createNestedArray("topic_sub");
  subTopics.add(topicAlertDhtExact);
  subTopics.add(topicAlertDhtWildcard);
  subTopics.add(topicSampling);

  doc["timestamp"] = "device-local-ts"; 

  String payload;
  serializeJson(doc, payload);

  String response;
  String url = String(CATALOG_BASE_URL) + "/devices/" + DEVICE_ID;
  return httpPatch(url, payload, response);
}

void constructTopics() {
  topicUp               = "SC/" + userId + "/" + roomId + "/dht";
  topicServo            = "SC/" + userId + "/" + roomId + "/ServoDHT";
  topicAlertDhtExact    = "SC/alerts/" + userId + "/" + roomId + "/dht";
  topicAlertDhtWildcard = "SC/alerts/+/+/dht";
  topicDown             = "SC/" + userId + "/" + roomId + "/down";
  topicSampling         = "SC/" + userId + "/" + roomId + "/sampling";
}

// ============================================================================
// WIFI & MQTT
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
      
      mqttClient.subscribe(topicAlertDhtExact.c_str(), 1);
      mqttClient.subscribe(topicAlertDhtWildcard.c_str(), 1);
      mqttClient.subscribe(topicSampling.c_str(), 1);
      
      Serial.printf("[mqtt] Subscribed: %s\n", topicAlertDhtExact.c_str());
      Serial.printf("[mqtt] Subscribed: %s\n", topicAlertDhtWildcard.c_str());
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

void publishActuatorState() {
  if (!USE_MQTT) return;
  String payload = "[{\"bn\":\"ServoState\",\"bt\":0,\"e\":[{\"n\":\"servoFan\",\"u\":\"bool\",\"vb\":";
  payload += (servoOn ? "true" : "false");
  payload += "}]}]";
  
  mqttClient.publish(topicServo.c_str(), payload.c_str());
  Serial.println("[mqtt] PUB Servo: " + payload);
}

void publishDeviceStatus(const char* statusText) {
  if (!USE_MQTT) return;
  char buf[256];
  snprintf(buf, sizeof(buf),
           "{\"device\":\"%s\",\"type\":\"dht\",\"status\":\"%s\",\"servoFan\":%s,\"sampling\":%s}",
           DEVICE_ID,
           statusText,
           servoOn ? "true" : "false", 
           samplingEnabled ? "true" : "false");
           
  mqttClient.publish(topicDown.c_str(), buf);
  Serial.printf("[mqtt] PUB Status: %s\n", buf);
}

bool isAlertTopic(const char* topic) {
  if (!topic) return false;
  if (strncmp(topic, "SC/alerts/", 10) != 0) return false;
  size_t len = strlen(topic);
  if (len < 4) return false;
  return (strcmp(topic + (len - 4), "/dht") == 0);
}

bool isAlertPayload(const char* json) {
  StaticJsonDocument<2048> doc;
  DeserializationError err = deserializeJson(doc, json);
  
  if (!err) {
    if (doc.containsKey("events") && doc["events"].is<JsonArray>()) {
      JsonArray events = doc["events"].as<JsonArray>();
      for (JsonObject e : events) {
        const char* status = e["status"] | "";
        if (strcmp(status, "ALERT") == 0) return true;
      }
    }
    const char* status = doc["status"] | "";
    if (strcmp(status, "ALERT") == 0) return true;
  }
  return (strstr(json, "\"status\":\"ALERT\"") != nullptr || strstr(json, "\"status\": \"ALERT\"") != nullptr);
}

void handleAlertMessage(const char* payload) {
  if (!samplingEnabled) return; 

  bool alertActive = isAlertPayload(payload);
  Serial.printf("[alert] Status: %s (Current servo: %s)\n", alertActive ? "ALERT" : "OK", servoOn ? "ON" : "OFF");

  if (alertActive != servoOn) {
    servoOn = alertActive;
    fan.attach(PIN_SERVO, 500, 2400);
    fan.write(servoOn ? 180 : 0);
    
    publishActuatorState();
    publishDeviceStatus(servoOn ? "ALERT" : "OK");
    Serial.printf("[actuator] Fan -> %s\n", servoOn ? "ON" : "OFF");
  }
}

void handleSamplingMessage(const char* payload) {
  StaticJsonDocument<256> doc;
  DeserializationError err = deserializeJson(doc, payload);
  if (err) return;

  bool enable = doc["enable"] | false;
  if (enable != samplingEnabled) {
    samplingEnabled = enable;
    
    if (!samplingEnabled && servoOn) {
      servoOn = false;
      fan.attach(PIN_SERVO, 500, 2400);
      fan.write(0);
      publishActuatorState();
    }
    
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

  if (isAlertTopic(topic)) {
    handleAlertMessage(msgBuf);
  } else if (topicSampling == topic) { 
    handleSamplingMessage(msgBuf);
  }
}

// ============================================================================
// MAIN SETUP & LOOP
// ============================================================================

void setup() {
  Serial.begin(115200);
  Serial.println("\n\n--- IoT Sensor Device Booting ---");

  // Init Hardware
  dht.setup(PIN_DHT, DHTesp::DHT22);
  fan.attach(PIN_SERVO, 500, 2400);
  fan.write(0);
  servoOn = false;

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
    publishActuatorState(); 
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

    TempAndHumidity th = dht.getTempAndHumidity();
    if (!isnan(th.temperature) && !isnan(th.humidity)) {
      String baseName = userId + "/" + roomId + "/";
      String senml = "[{\"bn\":\"" + baseName + "\",\"bt\":0,\"e\":["
                     "{\"n\":\"temp\",\"u\":\"Cel\",\"v\":" + String(th.temperature, 1) + "},"
                     "{\"n\":\"hum\",\"u\":\"%RH\",\"v\":"   + String(th.humidity, 1)    + "}"
                     "]}]";
      
      if (USE_MQTT) {
        mqttClient.publish(topicUp.c_str(), senml.c_str());
      }
      Serial.println("[telemetry] PUB: " + senml);
    }
  }
}
