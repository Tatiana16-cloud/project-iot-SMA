#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <math.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "nvs_flash.h"
#include "esp_netif.h"
#include "esp_event.h"
#include "protocol_examples_common.h"
#include "esp_http_client.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "mqtt_client.h"
#include "driver/gpio.h"
#include "driver/ledc.h"
#include "esp_timer.h"
#include "cJSON.h"

#define TAG "ESP_PPG"

// ============================================================================
// CONFIGURATION
// ============================================================================

#define WIFI_SSID               "Wokwi-GUEST"
#define WIFI_PASS               ""
#define DEVICE_ID               "ESP1"
#define CATALOG_BASE_URL        "https://moody-tables-sits-brian.trycloudflare.com" // Update as needed
#define MQTT_BROKER_URI         "mqtt://broker.hivemq.com:1883"

// Hardware Pins (ESP32-S3 default)
#define PULSE_ADC_CH            ADC_CHANNEL_6   // GPIO7
#define LED_GPIO                4
#define BUZZER_GPIO             5
#define BUZZER_PASSIVE          1               // 1=PWM, 0=Active

// PPG Parameters
#define PPG_FS_HZ               100
#define PPG_DT_MS               (1000/PPG_FS_HZ)
#define PEAK_REFRACT_MS         300
#define TH_K                    2.5f

// ============================================================================
// GLOBALS
// ============================================================================

char user_id[64] = "";
char room_id[64] = "";

// Dynamic Topics
char topic_up[128];
char topic_down[128];
char topic_alert_hr[128];
char topic_wakeup[128];
char topic_sampling[128];

// State
static bool led_on = false;
static bool buzzer_on_state = false;
static volatile bool hr_alarm_active = false;
static volatile int64_t wake_alarm_until_ms = 0;
static bool sampling_enabled = false;

static esp_mqtt_client_handle_t client = NULL;

// ============================================================================
// HARDWARE HELPERS
// ============================================================================

static void buzzer_pwm_init(void) {
#if BUZZER_PASSIVE
    ledc_timer_config_t tcfg = {
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .timer_num = LEDC_TIMER_0,
        .duty_resolution = LEDC_TIMER_10_BIT,
        .freq_hz = 2000,
        .clk_cfg = LEDC_AUTO_CLK
    };
    ledc_timer_config(&tcfg);

    ledc_channel_config_t ccfg = {
        .gpio_num   = BUZZER_GPIO,
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .channel    = LEDC_CHANNEL_0,
        .intr_type  = LEDC_INTR_DISABLE,
        .timer_sel  = LEDC_TIMER_0,
        .duty       = 0,
        .hpoint     = 0
    };
    ledc_channel_config(&ccfg);
#endif
}

static void buzzer_on(void){
#if BUZZER_PASSIVE
    ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0, 512);
    ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0);
#else
    gpio_set_level(BUZZER_GPIO, 1);
#endif
}

static void buzzer_off(void){
#if BUZZER_PASSIVE
    ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0, 0);
    ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0);
#else
    gpio_set_level(BUZZER_GPIO, 0);
#endif
}

static void apply_outputs(void) {
    int64_t now = esp_timer_get_time() / 1000;
    bool alarm_condition = hr_alarm_active || (now < wake_alarm_until_ms);

    if (alarm_condition && (!led_on || !buzzer_on_state)) {
        gpio_set_level(LED_GPIO, 1);
        buzzer_on();
        led_on = true;
        buzzer_on_state = true;
        ESP_LOGI(TAG, "ALARM ON");
    } else if (!alarm_condition && (led_on || buzzer_on_state)) {
        gpio_set_level(LED_GPIO, 0);
        buzzer_off();
        led_on = false;
        buzzer_on_state = false;
        ESP_LOGI(TAG, "ALARM OFF");
    }
}

// ============================================================================
// NETWORK HELPERS (HTTP)
// ============================================================================

esp_err_t _http_event_handler(esp_http_client_event_t *evt) {
    static char *output_buffer; 
    static int output_len;
    switch(evt->event_id) {
        case HTTP_EVENT_ON_DATA:
            if (!esp_http_client_is_chunked_response(evt->client)) {
                if (evt->user_data) {
                    // Append data to user buffer
                    char **buf_ptr = (char **)evt->user_data;
                    int current_len = *buf_ptr ? strlen(*buf_ptr) : 0;
                    *buf_ptr = realloc(*buf_ptr, current_len + evt->data_len + 1);
                    if (*buf_ptr) {
                        memcpy(*buf_ptr + current_len, evt->data, evt->data_len);
                        (*buf_ptr)[current_len + evt->data_len] = '\0';
                    }
                }
            }
            break;
        default:
            break;
    }
    return ESP_OK;
}

char* http_get(const char* url) {
    char *response_buffer = NULL;
    esp_http_client_config_t config = {
        .url = url,
        .event_handler = _http_event_handler,
        .user_data = &response_buffer,
        .timeout_ms = 5000,
        .crt_bundle_attach = NULL, // Insecure for dev
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    esp_err_t err = esp_http_client_perform(client);

    if (err == ESP_OK) {
        ESP_LOGI(TAG, "HTTP GET Status = %d, content_length = %lld",
                 esp_http_client_get_status_code(client),
                 esp_http_client_get_content_length(client));
    } else {
        ESP_LOGE(TAG, "HTTP GET request failed: %s", esp_err_to_name(err));
        if (response_buffer) { free(response_buffer); response_buffer = NULL; }
    }
    esp_http_client_cleanup(client);
    return response_buffer;
}

bool http_patch(const char* url, const char* json_data) {
    bool success = false;
    esp_http_client_config_t config = {
        .url = url,
        .method = HTTP_METHOD_PATCH,
        .timeout_ms = 5000,
        .crt_bundle_attach = NULL,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    esp_http_client_set_header(client, "Content-Type", "application/json");
    esp_http_client_set_post_field(client, json_data, strlen(json_data));
    
    esp_err_t err = esp_http_client_perform(client);
    if (err == ESP_OK) {
        int status = esp_http_client_get_status_code(client);
        if (status >= 200 && status < 300) success = true;
        ESP_LOGI(TAG, "HTTP PATCH Status = %d", status);
    } else {
        ESP_LOGE(TAG, "HTTP PATCH request failed: %s", esp_err_to_name(err));
    }
    esp_http_client_cleanup(client);
    return success;
}

// ============================================================================
// CATALOG LOGIC
// ============================================================================

bool resolve_identity() {
    char url[256];
    snprintf(url, sizeof(url), "%s/rooms", CATALOG_BASE_URL);
    
    char *response = http_get(url);
    if (!response) return false;

    cJSON *root = cJSON_Parse(response);
    free(response);
    if (!root) return false;

    bool found = false;
    if (cJSON_IsArray(root)) {
        cJSON *room;
        cJSON_ArrayForEach(room, root) {
            cJSON *devices = cJSON_GetObjectItem(room, "connected_devices");
            if (cJSON_IsArray(devices)) {
                cJSON *dev;
                cJSON_ArrayForEach(dev, devices) {
                    cJSON *did = cJSON_GetObjectItem(dev, "deviceID");
                    if (cJSON_IsString(did) && strcmp(did->valuestring, DEVICE_ID) == 0) {
                        cJSON *rid = cJSON_GetObjectItem(room, "roomID");
                        cJSON *uid = cJSON_GetObjectItem(room, "userID");
                        if (cJSON_IsString(rid) && cJSON_IsString(uid)) {
                            strncpy(room_id, rid->valuestring, sizeof(room_id)-1);
                            strncpy(user_id, uid->valuestring, sizeof(user_id)-1);
                            found = true;
                        }
                    }
                    if (found) break;
                }
            }
            if (found) break;
        }
    }
    cJSON_Delete(root);
    
    if (found) {
        ESP_LOGI(TAG, "Identity Resolved: User=%s, Room=%s", user_id, room_id);
    } else {
        ESP_LOGW(TAG, "Device ID %s not found in catalog", DEVICE_ID);
    }
    return found;
}

void construct_topics() {
    snprintf(topic_up, sizeof(topic_up), "SC/%s/%s/hr", user_id, room_id);
    snprintf(topic_down, sizeof(topic_down), "SC/%s/%s/down", user_id, room_id);
    snprintf(topic_alert_hr, sizeof(topic_alert_hr), "SC/alerts/%s/%s/hr", user_id, room_id);
    snprintf(topic_wakeup, sizeof(topic_wakeup), "SC/%s/%s/wakeup", user_id, room_id);
    snprintf(topic_sampling, sizeof(topic_sampling), "SC/%s/%s/sampling", user_id, room_id);
}

void update_catalog() {
    cJSON *root = cJSON_CreateObject();
    
    cJSON *avail = cJSON_AddArrayToObject(root, "availableServices");
    cJSON_AddItemToArray(avail, cJSON_CreateString("MQTT"));
    
    cJSON *details = cJSON_AddObjectToObject(root, "servicesDetails");
    cJSON_AddStringToObject(details, "serviceType", "MQTT");
    
    cJSON *pubs = cJSON_AddArrayToObject(details, "topic_pub");
    cJSON_AddItemToArray(pubs, cJSON_CreateString(topic_up));
    cJSON_AddItemToArray(pubs, cJSON_CreateString(topic_down));
    
    cJSON *subs = cJSON_AddArrayToObject(details, "topic_sub");
    cJSON_AddItemToArray(subs, cJSON_CreateString(topic_alert_hr));
    cJSON_AddItemToArray(subs, cJSON_CreateString(topic_wakeup));
    cJSON_AddItemToArray(subs, cJSON_CreateString(topic_sampling));
    
    cJSON_AddStringToObject(root, "timestamp", "device-local-ts");

    char *json_str = cJSON_PrintUnformatted(root);
    char url[256];
    snprintf(url, sizeof(url), "%s/devices/%s", CATALOG_BASE_URL, DEVICE_ID);
    
    http_patch(url, json_str);
    
    free(json_str);
            cJSON_Delete(root);
        }

// ============================================================================
// MQTT HANDLERS
// ============================================================================

static void publish_status(const char* status_msg) {
    if (!client) return;
    char buf[256];
    snprintf(buf, sizeof(buf), 
        "{\"device\":\"%s\",\"type\":\"hr\",\"status\":\"%s\",\"led\":%s,\"buzzer\":%s,\"sampling\":%s}",
        DEVICE_ID, status_msg, led_on?"true":"false", buzzer_on_state?"true":"false", sampling_enabled?"true":"false");
    esp_mqtt_client_publish(client, topic_down, buf, 0, 1, 0);
}

static void handle_mqtt_data(esp_mqtt_event_handle_t event) {
    char topic[128];
    char *payload = malloc(event->data_len + 1);
    if (!payload) return;
    
    int t_len = (event->topic_len < sizeof(topic)-1) ? event->topic_len : sizeof(topic)-1;
    memcpy(topic, event->topic, t_len);
    topic[t_len] = 0;
    memcpy(payload, event->data, event->data_len);
    payload[event->data_len] = 0;

    ESP_LOGI(TAG, "MQTT RX: %s", topic);

    cJSON *root = cJSON_Parse(payload);
    if (!root) { free(payload); return; }

    if (strcmp(topic, topic_alert_hr) == 0) {
        bool alert = false;
        cJSON *status = cJSON_GetObjectItem(root, "status");
        if (cJSON_IsString(status) && strcmp(status->valuestring, "ALERT") == 0) alert = true;
        
        // Check "events" array if present
        if (!alert) {
            cJSON *evs = cJSON_GetObjectItem(root, "events");
            if (cJSON_IsArray(evs)) {
                cJSON *e;
                cJSON_ArrayForEach(e, evs) {
                    cJSON *s = cJSON_GetObjectItem(e, "status");
                    if (cJSON_IsString(s) && strcmp(s->valuestring, "ALERT") == 0) {
                        alert = true; break;
                    }
                }
            }
        }
        hr_alarm_active = alert;
        apply_outputs();
        publish_status(alert ? "ALERT" : "OK");
    
    } else if (strcmp(topic, topic_wakeup) == 0) {
        int seconds = 30;
        cJSON *sec = cJSON_GetObjectItem(root, "seconds");
        if (cJSON_IsNumber(sec)) seconds = sec->valueint;
        
        int64_t now = esp_timer_get_time() / 1000;
        wake_alarm_until_ms = now + (seconds * 1000);
        apply_outputs();
        publish_status("WAKEUP");

    } else if (strcmp(topic, topic_sampling) == 0) {
        cJSON *en = cJSON_GetObjectItem(root, "enable");
        if (cJSON_IsBool(en)) {
            sampling_enabled = cJSON_IsTrue(en);
            ESP_LOGI(TAG, "Sampling set to %d", sampling_enabled);
            publish_status(sampling_enabled ? "MONITORING_ON" : "MONITORING_OFF");
        }
    }

    cJSON_Delete(root);
    free(payload);
}

static void mqtt_event_handler(void *handler_args, esp_event_base_t base, int32_t event_id, void *event_data) {
    esp_mqtt_event_handle_t event = event_data;
    switch ((esp_mqtt_event_id_t)event_id) {
        case MQTT_EVENT_CONNECTED:
            ESP_LOGI(TAG, "MQTT Connected");
            esp_mqtt_client_subscribe(client, topic_alert_hr, 1);
            esp_mqtt_client_subscribe(client, topic_wakeup, 1);
            esp_mqtt_client_subscribe(client, topic_sampling, 1);
            publish_status("ONLINE");
            break;
        case MQTT_EVENT_DATA:
            handle_mqtt_data(event);
            break;
        default:
            break;
    }
}

static void mqtt_start(void) {
    esp_mqtt_client_config_t mqtt_cfg = {
        .broker.address.uri = MQTT_BROKER_URI,
    };
    client = esp_mqtt_client_init(&mqtt_cfg);
    esp_mqtt_client_register_event(client, ESP_EVENT_ANY_ID, mqtt_event_handler, NULL);
    esp_mqtt_client_start(client);
}

// ============================================================================
// MAIN APP
// ============================================================================

void app_main(void) {
    // Init NVS & WiFi
    ESP_ERROR_CHECK(nvs_flash_init());
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    ESP_ERROR_CHECK(example_connect()); // Configured via menuconfig

    // GPIO Init
    gpio_config_t io_conf = {
        .pin_bit_mask = (1ULL << LED_GPIO) | (1ULL << BUZZER_GPIO),
        .mode = GPIO_MODE_OUTPUT,
    };
    gpio_config(&io_conf);
    gpio_set_level(LED_GPIO, 0);
    gpio_set_level(BUZZER_GPIO, 0);
    buzzer_pwm_init();

    // Catalog Flow
    if (resolve_identity()) {
        construct_topics();
        update_catalog();
    } else {
        ESP_LOGW(TAG, "Identity resolution failed. Using fallback topics.");
        strncpy(user_id, "{User1}", sizeof(user_id));
        strncpy(room_id, "{Room1}", sizeof(room_id));
        construct_topics();
    }

    // Start MQTT
    mqtt_start();

    // ADC Init
    adc_oneshot_unit_handle_t adc_handle;
    adc_oneshot_unit_init_cfg_t init_config = { .unit_id = ADC_UNIT_1 };
    ESP_ERROR_CHECK(adc_oneshot_new_unit(&init_config, &adc_handle));

    adc_oneshot_chan_cfg_t config = {
        .bitwidth = ADC_BITWIDTH_DEFAULT,
        .atten = ADC_ATTEN_DB_12,
    };
    ESP_ERROR_CHECK(adc_oneshot_config_channel(adc_handle, PULSE_ADC_CH, &config));

    // PPG Loop
    float mean = 0, mad = 1;
    float a_mean = 0.01f, a_mad = 0.01f;
    float prev_x = 0;
    int64_t last_peak_ms = 0;
    int bpm = 0;
    char payload[256];

    while (1) {
        if (!sampling_enabled) {
            apply_outputs(); // Keep checking alarms even if sampling is off
            vTaskDelay(pdMS_TO_TICKS(500));
            continue;
        }

        // Sampling Burst (1 sec)
        for (int i = 0; i < PPG_FS_HZ; i++) {
            int raw;
            ESP_ERROR_CHECK(adc_oneshot_read(adc_handle, PULSE_ADC_CH, &raw));
            float x = (float)raw; // Simplified, raw is enough for peak detect

            float err = x - mean;
            mean += a_mean * err;
            mad += a_mad * (fabsf(err) - mad);
            float th = mean + TH_K * (mad > 1 ? mad : 1);

            int64_t now_ms = esp_timer_get_time() / 1000;
            if (x > th && prev_x <= th && (now_ms - last_peak_ms) > PEAK_REFRACT_MS) {
                if (last_peak_ms != 0) {
                    int dt = (int)(now_ms - last_peak_ms);
                    int bpm_inst = dt > 0 ? 60000 / dt : 0;
                    if (bpm_inst >= 40 && bpm_inst <= 180) bpm = bpm_inst;
                }
                last_peak_ms = now_ms;
            }
            prev_x = x;
            vTaskDelay(pdMS_TO_TICKS(PPG_DT_MS));
        }

        apply_outputs();

        // Publish SenML
        snprintf(payload, sizeof(payload),
            "[{\"bn\":\"%s/%s/\",\"bt\":0,\"e\":[{\"n\":\"bpm\",\"u\":\"beats/min\",\"v\":%d}]}]",
            user_id, room_id, bpm);
            
        esp_mqtt_client_publish(client, topic_up, payload, 0, 1, 0);
        ESP_LOGI(TAG, "PUB BPM: %d", bpm);
    }
}
