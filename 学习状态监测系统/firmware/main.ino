#include "esp_camera.h"
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <WebServer.h>
#include <WiFiMulti.h>
#include "config.h"

// AI Thinker ESP32-CAM 摄像头引脚
#define PWDN_GPIO_NUM     32
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM      0
#define SIOD_GPIO_NUM     26
#define SIOC_GPIO_NUM     27
#define Y9_GPIO_NUM       35
#define Y8_GPIO_NUM       34
#define Y7_GPIO_NUM       39
#define Y6_GPIO_NUM       36
#define Y5_GPIO_NUM       21
#define Y4_GPIO_NUM       19
#define Y3_GPIO_NUM       18
#define Y2_GPIO_NUM        5
#define VSYNC_GPIO_NUM    25
#define HREF_GPIO_NUM     23
#define PCLK_GPIO_NUM     22

enum State {
  STATE_NORMAL,
  STATE_BLOWING,
  STATE_SNOOZE
};

static State    g_state           = STATE_NORMAL;
static uint32_t g_lastUploadMs    = 0;
static uint32_t g_blowStartMs     = 0;
static uint32_t g_snoozeStartMs   = 0;

static bool     g_btnLastReading  = HIGH;
static bool     g_btnStable       = HIGH;
static uint32_t g_btnLastChange   = 0;

static bool     g_camOk           = false;   // 摄像头当前是否可用
static uint32_t g_camRetryMs      = 0;       // 上次重试摄像头的时间

WebServer webServer(80);   // 调试用：GET /capture 返回当前帧 JPEG
WiFiMulti wifiMulti;         // 多 WiFi：自动选信号最好的已知网络

bool initCameraOnce();
void setupWiFi();
void setupFan();
void setFan(uint8_t speed);
void setLed(bool on);
bool connectWiFi();
bool uploadAndHandle();
void enterBlowing();
void enterSnooze();
void enterNormal();
void handleButton();
void onButtonPressed();
void tickStateMachine();
void handleCapture();
void handleStatus();

void setup() {
  Serial.begin(115200);
  Serial.setDebugOutput(false);
  delay(200);
  LOGLN();
  LOGLN(F("[BOOT] posture monitor stage 1"));

  pinMode(PIN_LED_RED, OUTPUT);
  setLed(false);
#if BUTTON_ENABLED
  pinMode(PIN_BUTTON, INPUT_PULLUP);
#endif
  setupFan();

  g_camOk = initCameraOnce();
  if (!g_camOk) {
    g_camRetryMs = millis();
    LOGLN(F("[BOOT] camera unavailable -> degraded mode, will retry"));
  }
  setupWiFi();

webServer.on("/capture", HTTP_GET, handleCapture);
  webServer.on("/status",  HTTP_GET, handleStatus);
  webServer.begin();
  LOGF("[WEB] capture: http://%s/capture\n", WiFi.localIP().toString().c_str());

  g_lastUploadMs = millis();
  LOGLN(F("[BOOT] ready, state=NORMAL"));
}

void loop() {
#if BUTTON_ENABLED
  handleButton();
#endif

  // 摄像头不可用：定期重试初始化；期间 WiFi 与 Web 服务保持可用
  if (!g_camOk && millis() - g_camRetryMs >= CAM_RETRY_INTERVAL_MS) {
    g_camRetryMs = millis();
    g_camOk = initCameraOnce();
    if (g_camOk) {
      LOGLN(F("[CAM] recovered, resume monitoring"));
      g_lastUploadMs = millis();
    }
  }

  tickStateMachine();
  webServer.handleClient();
}

bool initCameraOnce() {
  camera_config_t config = {};            // 必须零初始化，否则 fb_location 等字段是栈垃圾
  config.ledc_channel = LEDC_CHANNEL_1;   // 风扇占用 channel 0
  config.ledc_timer   = LEDC_TIMER_1;
  config.pin_d0       = Y2_GPIO_NUM;
  config.pin_d1       = Y3_GPIO_NUM;
  config.pin_d2       = Y4_GPIO_NUM;
  config.pin_d3       = Y5_GPIO_NUM;
  config.pin_d4       = Y6_GPIO_NUM;
  config.pin_d5       = Y7_GPIO_NUM;
  config.pin_d6       = Y8_GPIO_NUM;
  config.pin_d7       = Y9_GPIO_NUM;
  config.pin_xclk     = XCLK_GPIO_NUM;
  config.pin_pclk     = PCLK_GPIO_NUM;
  config.pin_vsync    = VSYNC_GPIO_NUM;
  config.pin_href     = HREF_GPIO_NUM;
  config.pin_sscb_sda = SIOD_GPIO_NUM;
  config.pin_sscb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn     = PWDN_GPIO_NUM;
  config.pin_reset    = RESET_GPIO_NUM;
  config.xclk_freq_hz = CAM_XCLK_FREQ;
  config.pixel_format = PIXFORMAT_JPEG;
  config.grab_mode    = CAMERA_GRAB_WHEN_EMPTY;

  if (psramFound()) {
    config.frame_size   = CAM_FRAME_SIZE;
    config.jpeg_quality = CAM_JPEG_QUALITY;
    config.fb_count     = 2;
    config.fb_location  = CAMERA_FB_IN_PSRAM;
  } else {
    config.frame_size   = FRAMESIZE_SVGA;
    config.jpeg_quality = 15;
    config.fb_count     = 1;
    config.fb_location  = CAMERA_FB_IN_DRAM;
  }

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    LOGF("[CAM] init failed: 0x%x (retry in %lu s)\n", err,
         (unsigned long)(CAM_RETRY_INTERVAL_MS / 1000));
    return false;
  }

  // 初始化返回 OK 不代表真能出图（排线接触不良时可能是半初始化状态）。
  // 抓一帧验证；失败则清理驱动状态，交给上层定期重试。
  camera_fb_t *probe = esp_camera_fb_get();
  if (!probe) {
    LOGLN(F("[CAM] init ok but capture failed, deinit and retry later"));
    esp_camera_deinit();
    return false;
  }
  esp_camera_fb_return(probe);

  LOGLN(F("[CAM] init ok"));

  // AI Thinker ESP32-CAM 的 OV3660 默认图像上下颠倒，必须翻转回来，
  // 否则姿态判定（head_height = 肩部 - 鼻子 > 0）会把坐姿判成驼背，导致每 60s 误吹风。
  // 该处理与已验证的 M1 CameraWebServer 保持一致；OV2640 板不受影响。
  sensor_t *s = esp_camera_sensor_get();
  if (s != NULL && s->id.PID == OV3660_PID) {
    s->set_vflip(s, 1);        // flip it back
    s->set_brightness(s, 1);   // up the brightness just a bit
    s->set_saturation(s, -2);  // lower the saturation
  }
  return true;
}

void setupWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);

  int added = 0;
  for (int i = 0; i < WIFI_NET_COUNT; i++) {
    if (WIFI_SSIDS[i] != NULL && strlen(WIFI_SSIDS[i]) > 0) {
      wifiMulti.addAP(WIFI_SSIDS[i], WIFI_PASSWORDS[i]);
      added++;
      LOGF("[WiFi] + known network: %s\n", WIFI_SSIDS[i]);
    }
  }
  LOGF("[WiFi] connecting, %d known network(s) ...\n", added);

  if (wifiMulti.run(WIFI_TIMEOUT_MS) == WL_CONNECTED) {
    LOGF("[WiFi] connected, IP: %s\n", WiFi.localIP().toString().c_str());
  } else {
    LOGLN(F("[WiFi] first connect timeout, will retry at runtime"));
  }
}

void setupFan() {
  // core 3.x API：ledcSetup/ledcAttachPin 已移除。
  // 用 ledcAttachChannel 显式占用 channel 0，摄像头固定用 channel 1，互不冲突。
  ledcAttachChannel(PIN_FAN, FAN_PWM_FREQ, FAN_PWM_RESOLUTION, FAN_PWM_CHANNEL);
  setFan(FAN_SPEED_OFF);
}

void setFan(uint8_t speed) {
  ledcWrite(PIN_FAN, speed);  // 3.x 下第一参数是引脚而非通道
}

void setLed(bool on) {
  digitalWrite(PIN_LED_RED, on ? HIGH : LOW);
}

bool connectWiFi() {
  if (WiFi.status() == WL_CONNECTED) return true;
  LOGLN(F("[WiFi] disconnected, reconnecting..."));
  if (wifiMulti.run(8000) == WL_CONNECTED) {
    LOGF("[WiFi] reconnected, IP: %s\n", WiFi.localIP().toString().c_str());
    return true;
  }
  return false;
}

void tickStateMachine() {
  uint32_t now = millis();

  switch (g_state) {
    case STATE_NORMAL:
      if (now - g_lastUploadMs >= UPLOAD_INTERVAL_MS) {
        g_lastUploadMs = now;
        if (g_camOk && connectWiFi()) {
          uploadAndHandle();
        }
      }
      break;

    case STATE_BLOWING:
      if (now - g_blowStartMs >= BLOW_DURATION_MS) {
        enterNormal();
      }
      break;

    case STATE_SNOOZE:
      if (now - g_snoozeStartMs >= SNOOZE_DURATION_MS) {
        LOGLN(F("[SNOOZE] over, resume"));
        enterNormal();
      }
      break;
  }
}

bool uploadAndHandle() {
  if (!g_camOk) return false;

  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) {
    LOGLN(F("[CAM] capture failed"));
    return false;
  }

  HTTPClient http;
  http.begin(SERVER_URL);
  http.setTimeout(HTTP_TIMEOUT_MS);
  http.addHeader("Content-Type", "image/jpeg");
  http.addHeader("X-Device-Key", DEVICE_KEY);

  int code = http.POST(fb->buf, fb->len);
  esp_camera_fb_return(fb);

  if (code != HTTP_CODE_OK) {
    LOGF("[HTTP] failed, code=%d\n", code);
    http.end();
    return false;
  }

  String payload = http.getString();
  http.end();

  JsonDocument doc;  // ArduinoJson v7：动态分配，容纳将来多态通道较长的 reason 文本
  DeserializationError err = deserializeJson(doc, payload);
  if (err) {
    LOGF("[JSON] parse failed: %s\n", err.c_str());
    return false;
  }

  const char *action = doc["action"] | "none";
  LOGF("[HTTP] resp: %s\n", payload.c_str());

  if (strcmp(action, "blow") == 0) {
    enterBlowing();
  }
  return true;
}

// 调试用抓图接口：GET /capture → 返回当前摄像头 JPEG（与上传共用帧缓冲，轮询驱动无并发）
void handleCapture() {
  if (!g_camOk) {
    webServer.send(503, "text/plain", "camera unavailable");
    return;
  }
  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) {
    webServer.send(500, "text/plain", "capture failed");
    return;
  }
  webServer.send_P(200, "image/jpeg", (const char *)fb->buf, fb->len);
  esp_camera_fb_return(fb);
}

// 状态查询接口：GET /status → JSON（摄像头是否可用、WiFi、IP、状态机、运行时长）
void handleStatus() {
  String s = "{";
  s += "\"cam_ok\":" + String(g_camOk ? "true" : "false");
  s += ",\"wifi\":" + String(WiFi.status() == WL_CONNECTED ? "true" : "false");
  s += ",\"ip\":\"" + WiFi.localIP().toString() + "\"";
  s += ",\"state\":" + String((int)g_state);
  s += ",\"uptime_s\":" + String(millis() / 1000);
  s += "}";
  webServer.send(200, "application/json", s);
}

void enterBlowing() {
  if (g_state == STATE_SNOOZE) return;
  LOGLN(F("[STATE] -> BLOWING (5s)"));
  g_state       = STATE_BLOWING;
  g_blowStartMs = millis();
  setFan(FAN_SPEED_ON);
  setLed(true);
}

void enterSnooze() {
  LOGLN(F("[STATE] -> SNOOZE (5min)"));
  g_state         = STATE_SNOOZE;
  g_snoozeStartMs = millis();
  setFan(FAN_SPEED_OFF);
  setLed(true);
}

void enterNormal() {
  LOGLN(F("[STATE] -> NORMAL"));
  g_state        = STATE_NORMAL;
  setFan(FAN_SPEED_OFF);
  setLed(false);
  g_lastUploadMs = millis();
}

void handleButton() {
  bool reading = digitalRead(PIN_BUTTON);

  if (reading != g_btnLastReading) {
    g_btnLastChange  = millis();
    g_btnLastReading = reading;
  }

  if (millis() - g_btnLastChange >= BUTTON_DEBOUNCE_MS) {
    if (reading != g_btnStable) {
      g_btnStable = reading;
      if (g_btnStable == LOW) {
        onButtonPressed();
      }
    }
  }
}

void onButtonPressed() {
  if (g_state == STATE_SNOOZE) {
    LOGLN(F("[BTN] cancel snooze"));
    enterNormal();
  } else {
    LOGLN(F("[BTN] snooze"));
    enterSnooze();
  }
}
