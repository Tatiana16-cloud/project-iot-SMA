#include <WiFi.h>
#include <PubSubClient.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
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
#define CATALOG_BASE_URL        "https://ran-members-moves-copies.trycloudflare.com" // Update with your tunnel URL
#define CATALOG_WRITE_TOKEN     ""

// ---- Flags ----
#define USE_WIFI                1
#define USE_CATALOG_LOOKUP      1
#define USE_MQTT                1

// ---- Hardware Pins ----
#define PIN_PPG                 34    // ADC input (potentiometer simulating PPG sensor)
#define PIN_LED                 4
#define PIN_BUZZER              5

// ---- Heart Rate Mapping ----
#define HR_MIN_BPM              60
#define HR_MAX_BPM              200
#define ADC_MAX                 4095

// ---- Timing ----
#define TELEMETRY_INTERVAL_MS   1000
#define WIFI_CONNECT_TIMEOUT_MS 10000
#define HTTP_TIMEOUT_MS         20000

// ============================================================================
// GLOBALS
// ============================================================================

WiFiClient   wifiClient;
PubSubClient mqttClient(wifiClient);

// Identity (userId/roomId resolved at runtime from catalog)
String userId = "";
String roomId = "";

// Dynamic MQTT Topics
String topicHr;
String topicDown;
String topicAlertHr;
String topicWakeup;
String topicSampling;
String topicAlarmOff;

// Alarm State
bool ledOn                     = false;
bool buzzerOn                  = false;
bool hrAlarmActive             = false;
unsigned long wakeAlarmUntilMs = 0;

// Sampling State
bool samplingEnabled           = false;

// Heart Rate State
int    currentBpm              = 0;
unsigned long lastTelemetryMs  = 0;

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
  pubTopics.add(topicHr);
  pubTopics.add(topicDown);

  JsonArray subTopics = svc.createNestedArray("topic_sub");
  subTopics.add(topicAlertHr);
  subTopics.add(topicWakeup);
  subTopics.add(topicSampling);
  subTopics.add(topicAlarmOff);

  doc["timestamp"] = "device-local-ts";

  String payload;
  serializeJson(doc, payload);

  String response;
  String url = String(CATALOG_BASE_URL) + "/devices/" + DEVICE_ID;
  return httpPatch(url, payload, response);
}

void constructTopics() {
  topicHr       = "SC/" + userId + "/" + roomId + "/hr";
  topicDown     = "SC/" + userId + "/" + roomId + "/down";
  topicAlertHr  = "SC/alerts/" + userId + "/" + roomId + "/hr";
  topicWakeup   = "SC/" + userId + "/" + roomId + "/wakeup";
  topicSampling = "SC/" + userId + "/" + roomId + "/sampling";
  topicAlarmOff = "SC/" + userId + "/" + roomId + "/alarm_off";
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

      mqttClient.subscribe(topicAlertHr.c_str(), 1);
      mqttClient.subscribe(topicWakeup.c_str(), 1);
      mqttClient.subscribe(topicSampling.c_str(), 1);
      mqttClient.subscribe(topicAlarmOff.c_str(), 1);

      Serial.printf("[mqtt] Subscribed: %s\n", topicAlertHr.c_str());
      Serial.printf("[mqtt] Subscribed: %s\n", topicWakeup.c_str());
      Serial.printf("[mqtt] Subscribed: %s\n", topicSampling.c_str());
      Serial.printf("[mqtt] Subscribed: %s\n", topicAlarmOff.c_str());
    } else {
      Serial.printf("[mqtt] Failed, rc=%d. Retry in %dms\n", mqttClient.state(), MQTT_RECONNECT_DELAY_MS);
      delay(MQTT_RECONNECT_DELAY_MS);
    }
  }
}

// ============================================================================
// APPLICATION LOGIC
// ============================================================================

void applyAlarmOutputs() {
  unsigned long now = millis();
  bool alarmCondition = hrAlarmActive || (wakeAlarmUntilMs > 0 && now < wakeAlarmUntilMs);

  if (alarmCondition && (!ledOn || !buzzerOn)) {
    digitalWrite(PIN_LED, HIGH);
    tone(PIN_BUZZER, 2000);
    ledOn = true;
    buzzerOn = true;
    Serial.println("[alarm] ALARM ON");
  } else if (!alarmCondition && (ledOn || buzzerOn)) {
    digitalWrite(PIN_LED, LOW);
    noTone(PIN_BUZZER);
    ledOn = false;
    buzzerOn = false;
    Serial.println("[alarm] ALARM OFF");
  }
}

void publishBpm() {
  if (!USE_MQTT || currentBpm == 0) return;

  String baseName = userId + "/" + roomId + "/";
  String senml = "[{\"bn\":\"" + baseName + "\",\"bt\":0,\"e\":["
                 "{\"n\":\"bpm\",\"u\":\"beats/min\",\"v\":" + String(currentBpm) + "}"
                 "]}]";

  mqttClient.publish(topicHr.c_str(), senml.c_str());
  Serial.printf("[telemetry] PUB BPM: %d\n", currentBpm);
}

void publishDeviceStatus(const char* statusText) {
  if (!USE_MQTT) return;
  char buf[256];
  snprintf(buf, sizeof(buf),
           "{\"device\":\"%s\",\"type\":\"hr\",\"status\":\"%s\",\"led\":%s,\"buzzer\":%s,\"sampling\":%s}",
           DEVICE_ID,
           statusText,
           ledOn ? "true" : "false",
           buzzerOn ? "true" : "false",
           samplingEnabled ? "true" : "false");

  mqttClient.publish(topicDown.c_str(), buf);
  Serial.printf("[mqtt] PUB Status: %s\n", buf);
}

bool isAlertPayload(const char* json) {
  StaticJsonDocument<2048> doc;
  DeserializationError err = deserializeJson(doc, json);

  if (!err) {
    const char* status = doc["status"] | "";
    if (strcmp(status, "ALERT") == 0) return true;

    if (doc.containsKey("events") && doc["events"].is<JsonArray>()) {
      for (JsonObject e : doc["events"].as<JsonArray>()) {
        const char* s = e["status"] | "";
        if (strcmp(s, "ALERT") == 0) return true;
      }
    }
  }
  return false;
}

void handleAlertHr(const char* payload) {
  bool alertActive = isAlertPayload(payload);
  hrAlarmActive = alertActive;
  applyAlarmOutputs();
  publishDeviceStatus(alertActive ? "ALERT" : "OK");
  Serial.printf("[alert] HR alarm: %s\n", alertActive ? "ACTIVE" : "CLEARED");
}

void handleWakeup(const char* payload) {
  StaticJsonDocument<256> doc;
  DeserializationError err = deserializeJson(doc, payload);

  int seconds = 30;
  if (!err) {
    seconds = doc["seconds"] | 30;
  }

  wakeAlarmUntilMs = millis() + ((unsigned long)seconds * 1000);
  applyAlarmOutputs();
  publishDeviceStatus("WAKEUP");
  Serial.printf("[alarm] Wakeup alarm for %d seconds\n", seconds);
}

void handleAlarmOff() {
  wakeAlarmUntilMs = 0;
  hrAlarmActive = false;
  applyAlarmOutputs();
  publishDeviceStatus("ALARM_OFF");
  Serial.println("[alarm] Remote alarm off received");
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

void readHeartRate() {
  int raw = analogRead(PIN_PPG);
  currentBpm = map(raw, 0, ADC_MAX, HR_MIN_BPM, HR_MAX_BPM);
  currentBpm = constrain(currentBpm, HR_MIN_BPM, HR_MAX_BPM);
}

void mqttCallback(char* topic, byte* payload, unsigned int len) {
  if (!USE_MQTT) return;

  static char msgBuf[2048];
  size_t copyLen = (len < sizeof(msgBuf)) ? len : (sizeof(msgBuf) - 1);
  memcpy(msgBuf, payload, copyLen);
  msgBuf[copyLen] = '\0';

  Serial.printf("[mqtt] RX %s (%d bytes)\n", topic, len);

  if (topicAlertHr == topic) {
    handleAlertHr(msgBuf);
  } else if (topicWakeup == topic) {
    handleWakeup(msgBuf);
  } else if (topicAlarmOff == topic) {
    handleAlarmOff();
  } else if (topicSampling == topic) {
    handleSamplingMessage(msgBuf);
  }
}

// ============================================================================
// MAIN SETUP & LOOP
// ============================================================================

void setup() {
  Serial.begin(115200);
  Serial.println("\n\n--- IoT PPG Sensor & Alarm Device Booting ---");

  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, LOW);
  pinMode(PIN_BUZZER, OUTPUT);
  digitalWrite(PIN_BUZZER, LOW);

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
    delay(1000);
    connectMQTT();
    publishDeviceStatus("ONLINE");
  }
}

void loop() {
  connectWiFi();

  if (USE_MQTT) {
    connectMQTT();
    mqttClient.loop();
  }

  applyAlarmOutputs();

  if (!samplingEnabled) {
    delay(200);
    return;
  }

  unsigned long now = millis();

  if (now - lastTelemetryMs >= TELEMETRY_INTERVAL_MS) {
    lastTelemetryMs = now;
    readHeartRate();
    publishBpm();
  }
}
