// ============================================================
// ESP32-S3-CAM 纯采集固件（标定/采样专用）
// ------------------------------------------------------------
// 特点：**开机自动探测多套相机引脚**，不需要事先知道板型。
//   引脚集合 0: Freenove ESP32-S3-WROOM CAM / Espressif ESP32-S3-EYE
//   引脚集合 1: Seeed XIAO ESP32S3 Sense
//   引脚集合 2: 常见"S3-CAM"克隆（Waveshare/Goouuu 等，D0-D7 反向）
// 哪套能出图就用哪套，探到后通过 /status 与串口告诉你。
//
// 编译参数（N16R8：16MB flash + 8MB OPI PSRAM）：
//   PSRAM=opi, FlashSize=16M, PartitionScheme=huge_app, CDCOnBoot=cdc
// ============================================================
#include "esp_camera.h"
#include <WiFi.h>
#include <WebServer.h>
#include "config.h"

struct PinSet {
  const char *name;
  int pwdn, reset, xclk, siod, sioc;
  int d7, d6, d5, d4, d3, d2, d1, d0;
  int vsync, href, pclk;
  int vflip;
};

// ---- 三套常见 S3-CAM 引脚 ----
static const PinSet PIN_SETS[] = {
  { "freenove/s3-eye", -1, -1, 15, 4, 5,  16, 17, 18, 12, 10, 8, 9, 11,  6, 7, 13, 0 },
  { "xiao-sense",      -1, -1, 10, 40, 39, 48, 11, 12, 14, 16, 18, 17, 15, 38, 47, 13, 1 },
  { "waveshare-s3cam", -1, -1, 15, 4, 5,  16, 17, 18, 12, 10, 8, 9, 11,  6, 7, 13, 1 },
};
static const int PIN_SET_COUNT = sizeof(PIN_SETS) / sizeof(PIN_SETS[0]);

static bool g_camOk = false;
static int  g_pinset = -1;
static uint32_t g_frames = 0;
WebServer server(80);

static bool tryInit(const PinSet &p) {
  camera_config_t c = {};
  c.ledc_channel = LEDC_CHANNEL_0;
  c.ledc_timer   = LEDC_TIMER_0;
  c.pin_pwdn  = p.pwdn;   c.pin_reset = p.reset;
  c.pin_xclk  = p.xclk;   c.pin_sccb_sda = p.siod;  c.pin_sccb_scl = p.sioc;
  c.pin_d7 = p.d7; c.pin_d6 = p.d6; c.pin_d5 = p.d5; c.pin_d4 = p.d4;
  c.pin_d3 = p.d3; c.pin_d2 = p.d2; c.pin_d1 = p.d1; c.pin_d0 = p.d0;
  c.pin_vsync = p.vsync; c.pin_href = p.href; c.pin_pclk = p.pclk;
  c.xclk_freq_hz = 20000000;
  c.pixel_format = PIXFORMAT_JPEG;
  c.grab_mode    = CAMERA_GRAB_LATEST;

  if (psramFound()) {
    c.frame_size   = FRAMESIZE_VGA;    // 640x480，与产线一致
    c.jpeg_quality = 12;
    c.fb_count     = 2;
    c.fb_location  = CAMERA_FB_IN_PSRAM;
  } else {
    c.frame_size   = FRAMESIZE_SVGA;
    c.jpeg_quality = 15;
    c.fb_count     = 1;
    c.fb_location  = CAMERA_FB_IN_DRAM;
  }

  esp_err_t err = esp_camera_init(&c);
  if (err != ESP_OK) {
    Serial.printf("[CAM] %s init failed 0x%x\n", p.name, err);
    return false;
  }

  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) {
    Serial.printf("[CAM] %s no frame\n", p.name);
    esp_camera_deinit();
    return false;
  }
  Serial.printf("[CAM] %s OK: %ux%u %u bytes\n", p.name, fb->width, fb->height, fb->len);
  esp_camera_fb_return(fb);

  sensor_t *s = esp_camera_sensor_get();
  if (s) {
    if (p.vflip) s->set_vflip(s, 1);
    s->set_brightness(s, 1);
    s->set_saturation(s, -2);
    Serial.printf("[CAM] sensor PID=0x%02x\n", s->id.PID);
  }
  return true;
}

static bool initCameraAuto() {
  for (int i = 0; i < PIN_SET_COUNT; i++) {
    if (tryInit(PIN_SETS[i])) { g_pinset = i; return true; }
  }
  return false;
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
  char buf[220];
  snprintf(buf, sizeof(buf),
           "{\"cam_ok\":%s,\"ip\":\"%s\",\"rssi\":%d,\"uptime_s\":%lu,\"frames\":%lu,"
           "\"psram\":%s,\"pinset\":\"%s\"}",
           g_camOk ? "true" : "false", WiFi.localIP().toString().c_str(),
           WiFi.RSSI(), (unsigned long)(millis() / 1000), (unsigned long)g_frames,
           psramFound() ? "true" : "false",
           g_pinset >= 0 ? PIN_SETS[g_pinset].name : "none");
  server.send(200, "application/json", buf);
}

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n[BOOT] ESP32-S3 capture-only (auto pinset)");
  Serial.printf("[SYS] psram=%s flash=%uMB\n", psramFound() ? "yes" : "no",
                (unsigned)(ESP.getFlashChipSize() / (1024 * 1024)));

  g_camOk = initCameraAuto();
  if (!g_camOk) Serial.println("[CAM] 所有引脚组合都失败 —— 检查排线/供电");

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.printf("[WiFi] %s", WIFI_SSID);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) { delay(500); Serial.print("."); }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[WiFi] OK ip=%s\n", WiFi.localIP().toString().c_str());
    Serial.printf("[USE]  http://%s/capture\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.println("[WiFi] FAILED —— 只支持 2.4GHz");
  }

  server.on("/capture", HTTP_GET, handleCapture);
  server.on("/status",  HTTP_GET, handleStatus);
  server.begin();
  Serial.println("[HTTP] :80 ready");
}

void loop() {
  server.handleClient();
  static uint32_t last = 0;
  if (millis() - last > 5000) {
    last = millis();
    if (!g_camOk) g_camOk = initCameraAuto();
  }
}
