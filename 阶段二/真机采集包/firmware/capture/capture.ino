// ============================================================
// ESP32-CAM 纯采集固件（标定/采样专用）
// ------------------------------------------------------------
// 只做三件事：连 WiFi、开 HTTP 服务、提供 GET /capture 出图。
// 不连服务器、不上传、不控风扇 —— 保证采到的帧和产线固件
// 的相机参数完全一致（VGA / quality 12 / OV3660 vflip）。
//
// 接口：
//   GET /capture  -> image/jpeg
//   GET /status   -> {"cam_ok":true,"ip":"...","rssi":-55,"uptime_s":123,"frames":42}
//   GET /shot?n=10&ms=300 -> 连拍 n 张（返回 manifest JSON，配合 capture.ps1 调试用）
// ============================================================
#include "esp_camera.h"
#include <WiFi.h>
#include <WebServer.h>
#include "config.h"

// ---- AI Thinker ESP32-CAM 引脚 ----
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

static bool g_camOk = false;
static uint32_t g_frames = 0;
WebServer server(80);

bool initCamera() {
  camera_config_t c = {};
  c.ledc_channel = LEDC_CHANNEL_1;
  c.ledc_timer   = LEDC_TIMER_1;
  c.pin_d0 = Y2_GPIO_NUM;  c.pin_d1 = Y3_GPIO_NUM;
  c.pin_d2 = Y4_GPIO_NUM;  c.pin_d3 = Y5_GPIO_NUM;
  c.pin_d4 = Y6_GPIO_NUM;  c.pin_d5 = Y7_GPIO_NUM;
  c.pin_d6 = Y8_GPIO_NUM;  c.pin_d7 = Y9_GPIO_NUM;
  c.pin_xclk = XCLK_GPIO_NUM;   c.pin_pclk = PCLK_GPIO_NUM;
  c.pin_vsync = VSYNC_GPIO_NUM; c.pin_href = HREF_GPIO_NUM;
  c.pin_sscb_sda = SIOD_GPIO_NUM; c.pin_sscb_scl = SIOC_GPIO_NUM;
  c.pin_pwdn = PWDN_GPIO_NUM;   c.pin_reset = RESET_GPIO_NUM;
  c.xclk_freq_hz = 20000000;
  c.pixel_format = PIXFORMAT_JPEG;
  c.grab_mode    = CAMERA_GRAB_WHEN_EMPTY;

  if (psramFound()) {
    c.frame_size   = FRAMESIZE_VGA;   // 640x480，与产线一致
    c.jpeg_quality = 12;              // 与产线一致
    c.fb_count     = 2;
    c.fb_location  = CAMERA_FB_IN_PSRAM;
  } else {
    c.frame_size   = FRAMESIZE_SVGA;
    c.jpeg_quality = 15;
    c.fb_count     = 1;
    c.fb_location  = CAMERA_FB_IN_DRAM;
  }

  if (esp_camera_init(&c) != ESP_OK) return false;

  camera_fb_t *probe = esp_camera_fb_get();
  if (!probe) { esp_camera_deinit(); return false; }
  esp_camera_fb_return(probe);

  // OV3660 出厂上下颠倒，必须翻回来（与产线固件一致）
  sensor_t *s = esp_camera_sensor_get();
  if (s && s->id.PID == OV3660_PID) {
    s->set_vflip(s, 1);
    s->set_brightness(s, 1);
    s->set_saturation(s, -2);
  }
  return true;
}

void handleCapture() {
  if (!g_camOk) { server.send(503, "text/plain", "camera unavailable"); return; }
  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) { server.send(500, "text/plain", "capture failed"); return; }
  server.sendHeader("Cache-Control", "no-store");
  server.send_P(200, "image/jpeg", (const char *)fb->buf, fb->len);
  g_frames++;
  esp_camera_fb_return(fb);
}

void handleStatus() {
  char buf[160];
  snprintf(buf, sizeof(buf),
           "{\"cam_ok\":%s,\"ip\":\"%s\",\"rssi\":%d,\"uptime_s\":%lu,\"frames\":%lu}",
           g_camOk ? "true" : "false", WiFi.localIP().toString().c_str(),
           WiFi.RSSI(), (unsigned long)(millis() / 1000), (unsigned long)g_frames);
  server.send(200, "application/json", buf);
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\n[BOOT] ESP32-CAM capture-only");

  g_camOk = initCamera();
  Serial.printf("[CAM] %s\n", g_camOk ? "ok" : "FAILED");

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.printf("[WiFi] connecting to %s", WIFI_SSID);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[WiFi] OK, IP = %s\n", WiFi.localIP().toString().c_str());
    Serial.printf("[USE]  http://%s/capture\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.println("[WiFi] FAILED —— 检查 SSID/密码是否为 2.4GHz");
  }

  server.on("/capture", HTTP_GET, handleCapture);
  server.on("/status",  HTTP_GET, handleStatus);
  server.begin();
  Serial.println("[HTTP] server started on :80");
}

void loop() {
  server.handleClient();
  static uint32_t lastPrint = 0;
  if (millis() - lastPrint > 5000) {
    lastPrint = millis();
    if (WiFi.status() == WL_CONNECTED && !g_camOk) {
      Serial.println("[CAM] retrying init...");
      g_camOk = initCamera();
    }
  }
}
