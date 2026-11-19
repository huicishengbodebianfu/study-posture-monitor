#include "esp_camera.h"
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <WebServer.h>
#include <WiFiMulti.h>
#include <Wire.h>
#include <U8g2lib.h>
#include <Preferences.h>
#include "config.h"

// ============================================================================
// ESP32-S3-CAM 摄像头引脚 —— 支持两种常见板型, 由 config.h 的 CAMERA_MODEL 宏切换
//
//   CAMERA_MODEL_ESP32S3_EYE     : Freenove FNK0085/FNK0086 (无屏, 40脚排针) ← 默认
//   CAMERA_MODEL_ESP32S3_CAM_LCD : ESP32-S3-CAM-LCD 带屏板
//
// 判定方法: 板子上有没有一块小 LCD 彩色屏?
//   - 没屏 → ESP32S3_EYE (默认, 不用改)
//   - 有屏 → 在 config.h 里把 CAMERA_MODEL 改成 CAMERA_MODEL_ESP32S3_CAM_LCD
// ============================================================================

#if defined(CAMERA_MODEL_ESP32S3_CAM_LCD)
  // ---- ESP32-S3-CAM-LCD 带屏板 ----
  #define PWDN_GPIO_NUM     -1
  #define RESET_GPIO_NUM    -1
  #define XCLK_GPIO_NUM     40
  #define SIOD_GPIO_NUM     17
  #define SIOC_GPIO_NUM     18
  #define Y9_GPIO_NUM       39      // D7
  #define Y8_GPIO_NUM       41      // D6
  #define Y7_GPIO_NUM       42      // D5
  #define Y6_GPIO_NUM       12      // D4
  #define Y5_GPIO_NUM       3       // D3
  #define Y4_GPIO_NUM       14      // D2
  #define Y3_GPIO_NUM       47      // D1
  #define Y2_GPIO_NUM       13      // D0
  #define VSYNC_GPIO_NUM    21
  #define HREF_GPIO_NUM     38
  #define PCLK_GPIO_NUM     11
#else
  // ---- 默认: CAMERA_MODEL_ESP32S3_EYE (Freenove FNK0085/FNK0086) ----
  // 来源: Freenove 官方 camera_pins.h (阶段二已从网盘下载核验)
  // 注意 D0..D7 非单调顺序: D0=11, D1=9, D2=8, D3=10, D4=12, D5=18, D6=17, D7=16
  #define PWDN_GPIO_NUM     -1      // 本板未接线, 必须 -1
  #define RESET_GPIO_NUM    -1      // 本板未接线, 必须 -1
  #define XCLK_GPIO_NUM     15
  #define SIOD_GPIO_NUM     4
  #define SIOC_GPIO_NUM     5

  #define Y9_GPIO_NUM       16      // D7
  #define Y8_GPIO_NUM       17      // D6
  #define Y7_GPIO_NUM       18      // D5
  #define Y6_GPIO_NUM       12      // D4
  #define Y5_GPIO_NUM       10      // D3
  #define Y4_GPIO_NUM       8       // D2
  #define Y3_GPIO_NUM       9       // D1
  #define Y2_GPIO_NUM       11      // D0

  #define VSYNC_GPIO_NUM    6
  #define HREF_GPIO_NUM     7
  #define PCLK_GPIO_NUM     13
#endif

// ============================================================================
// 状态机(与阶段一一致): NORMAL / BLOWING / SNOOZE
// ============================================================================
enum State {
  STATE_NORMAL,
  STATE_BLOWING,
  STATE_SNOOZE
};

// 状态灯颜色(双色共阴 LED: 红+绿 -> 黄)
enum LedColor {
  LED_OFF,      // 灭
  LED_GREEN,    // 绿常亮 = 正常
  LED_YELLOW,   // 黄(红+绿) = 降级/自检失败
  LED_RED       // 红 = 免打扰 / 吹风提醒
};

static State    g_state           = STATE_NORMAL;
static uint32_t g_lastUploadMs    = 0;
static uint32_t g_blowStartMs     = 0;
static uint32_t g_snoozeStartMs   = 0;

static bool     g_camOk           = false;   // 摄像头当前是否可用
static uint32_t g_camRetryMs      = 0;       // 上次重试摄像头的时间

// 安静开关(拨动开关, 电平检测, 无消抖定时器)
static bool     g_quietActive     = false;   // 当前是否处于安静位

// 电池电量(阶段二新增)
static float    g_batteryVoltage  = 0.0f;    // 最近一次采样电压
static bool     g_batteryLow      = false;   // 低电量标志
static bool     g_batteryValid    = false;   // 是否接了分压(未接时读数无意义)
static uint32_t g_batLastCheckMs  = 0;

// 摄像头可调参数(运行时, 网页调, 不用重烧)
static uint8_t  g_xclkMhz         = 10;      // XCLK 频率(MHz), 默认 10
static int      g_camPid          = 0;       // 传感器 PID(便于识别真实型号)

// ---- 有人/无人显示(来自 /detect v2 的 scene 字段) ----
enum Presence {
  PRESENCE_BOOT,      // 刚上电, 还没收到第一帧结果
  PRESENCE_PERSON,    // 有人
  PRESENCE_EMPTY,     // 无人(空座位)
  PRESENCE_OCCLUDED,  // 看不清(遮挡/太暗)
  PRESENCE_UNKNOWN    // 服务器没给 scene(老版本)
};
static Presence g_presence      = PRESENCE_BOOT;
static uint32_t g_lastDetectOkMs = 0;         // 最近一次成功响应的时刻
static bool     g_hasDetectOk   = false;      // 是否收到过成功响应
static uint8_t  g_emptyStreak   = 0;          // 连续 empty 帧数
static uint8_t  g_occlStreak    = 0;          // 连续 occluded 帧数
static bool     g_personFlag    = false;      // 服务器 person 字段
static char     g_sceneBuf[16]  = "";         // 服务器 scene 字段(person/empty/occluded)
static char     g_displayBuf[8] = "";         // 服务器 display 短码(UP/SL/AWAY/DIM/...)

// 设备自检结果(阶段二新增)
static struct {
  bool ran;              // 是否已跑过开机自检
  bool cam;              // 摄像头
  bool fan;              // 风扇通路(脉冲测试)
  bool led;              // LED 通路
  bool wifi;             // WiFi 连接
} g_selftest = { false, false, false, false, false };

// 连续上传失败计数(阶段二新增, 用于黄灯告警)
static int      g_uploadFailStreak = 0;

WebServer webServer(80);   // 调试用：GET /capture 返回当前帧 JPEG, GET /status 返回状态
WiFiMulti wifiMulti;       // 多 WiFi：自动选信号最好的已知网络

// 0.96 OLED (I2C SSD1306 128x64)
// ⚠️ 关键: 必须在构造函数里传 clock/data 引脚!
// 否则 U8g2 内部会调用无参 Wire.begin(), I2C 回到默认 GPIO8/9(摄像头脚) → OLED 黑屏。
// 构造函数签名: (rotation, reset, clock, data)
// 两个实例分别覆盖 SDA/SCL 正常接线 与 接反 两种情况:
U8G2_SSD1306_128X64_NONAME_F_HW_I2C u8g2A(U8G2_R0, U8X8_PIN_NONE, PIN_OLED_SCL, PIN_OLED_SDA);
U8G2_SSD1306_128X64_NONAME_F_HW_I2C u8g2B(U8G2_R0, U8X8_PIN_NONE, PIN_OLED_SDA, PIN_OLED_SCL);
U8G2 *oled = &u8g2A;                 // 实际使用哪个, 由 oledInit() 探测后决定

static uint8_t g_oledAddr  = OLED_I2C_ADDR;   // 实际命中的 I2C 地址
static bool    g_oledOk    = false;           // OLED 是否初始化成功

static uint32_t g_oledLastRefreshMs = 0;   // OLED 上次刷新时间(降低刷新频率, 避免闪烁/耗电)

// ---- 图像朝向(运行时可选, 掉电保存) ----
// 不同批次/不同厂家的摄像头模组安装方向不一样, 靠猜容易反, 所以做成运行时可切换:
//   0 = 不翻转
//   1 = 上下翻转 (vflip)
//   2 = 左右镜像 (hmirror)
//   3 = 上下+左右 (180° 旋转)
// 通过网页 /rotate 切换, 自动存进 NVS flash, 重启后保持。
Preferences g_prefs;
static uint8_t g_rotation = 0;

bool initCameraOnce();
void setupWiFi();
void setupFan();
void setFan(uint8_t speed);
void setLedColor(LedColor c);
void setLedPins(bool red, bool green);
bool connectWiFi();
bool uploadAndHandle();
void enterBlowing();
void enterSnooze();
void enterNormal();
void updateQuietSwitch();
void updateBattery();
void runSelfTest();
void tickStateMachine();
void refreshOled();
bool oledInit();
void applyRotation(uint8_t idx);
void loadRotation();
void handleCapture();
void handleStatus();
void handleRotate();
void handleI2cScan();
void handleCam();
void handleXclk();

// ============================================================================
// LED 控制(双色共阴 LED)
// ============================================================================
void setLedPins(bool red, bool green) {
  digitalWrite(PIN_LED_RED,   red   ? HIGH : LOW);
  digitalWrite(PIN_LED_GREEN, green ? HIGH : LOW);
}

void setLedColor(LedColor c) {
  switch (c) {
    case LED_OFF:    setLedPins(false, false); break;
    case LED_GREEN:  setLedPins(false, true);  break;
    case LED_YELLOW: setLedPins(true,  true);  break;
    case LED_RED:    setLedPins(true,  false); break;
  }
}

// 根据当前状态刷新状态灯(非阻塞, 在 loop 中调用)
void refreshStatusLed() {
  if (g_state == STATE_BLOWING) {
    // 吹风提醒: 红闪烁(1Hz)
    bool on = (millis() / 500) % 2 == 0;
    setLedPins(on, false);
    return;
  }
  if (g_state == STATE_SNOOZE || g_quietActive) {
    // 免打扰/安静: 红常亮
    setLedColor(LED_RED);
    return;
  }
  if (!g_camOk || g_uploadFailStreak >= 3) {
    // 降级/告警: 黄常亮(USB直供无电池, 不再用低电量触发)
    setLedColor(LED_YELLOW);
    return;
  }
  // 正常: 绿常亮
  setLedColor(LED_GREEN);
}

// ============================================================================
// OLED 显示(0.96寸 128x64 SSD1306) —— 中文
// ⚠️ 中文字体用 u8g2_font_wqy12_t_gb2312(12x12) + drawUTF8, 源码必须 UTF-8 编码
// ============================================================================
const char* stateLabel() {
  if (g_state == STATE_BLOWING)                          return "吹风提醒";
  if (g_state == STATE_SNOOZE || g_quietActive)          return "免打扰中";
  if (!g_camOk)                                          return "摄像头故障";
  if (g_uploadFailStreak >= 3)                           return "网络异常";
  return "正常监测";
}

// 第 4 行: 有人 / 无人 / 看不清 / 未知 / 离线 / 检测中
const char* presenceLabel() {
  if (!g_camOk) return "—";
  if (g_hasDetectOk && (millis() - g_lastDetectOkMs > OFFLINE_TIMEOUT_MS)) return "离线";
  switch (g_presence) {
    case PRESENCE_PERSON:   return "有人";
    case PRESENCE_EMPTY:    return "无人";
    case PRESENCE_OCCLUDED: return "看不清";
    case PRESENCE_UNKNOWN:  return "未知";
    case PRESENCE_BOOT:
    default:                return "检测中";
  }
}

void refreshOled() {
  if (!g_oledOk) return;
  uint32_t now = millis();
  // OLED 每 1 秒刷一次(内容变化慢, 太频繁会闪烁且耗电)
  if (now - g_oledLastRefreshMs < 1000) return;
  g_oledLastRefreshMs = now;

  char line[32];
  oled->clearBuffer();

  // 标题(中文, 12x12)
  oled->setFont(u8g2_font_wqy12_t_gb2312);
  oled->drawUTF8(0, 12, "学习坐姿监测");

  // 状态行(大字, 16x16 中文字体)
  oled->setFont(u8g2_font_wqy16_t_gb2312);
  oled->drawUTF8(0, 32, stateLabel());

  // WiFi 行(英文数字用 6x10)
  oled->setFont(u8g2_font_6x10_tf);
  if (WiFi.status() == WL_CONNECTED) {
    snprintf(line, sizeof(line), "WiFi %s", WiFi.localIP().toString().c_str());
  } else {
    snprintf(line, sizeof(line), "WiFi --");
  }
  oled->drawStr(0, 46, line);

#if BAT_ENABLED
  // 电量行(启用电池监测时)
  {
    int pct = (int)((g_batteryVoltage - BAT_LOW_VOLTAGE) / (BAT_FULL_VOLTAGE - BAT_LOW_VOLTAGE) * 100.0f);
    if (pct < 0) pct = 0;
    if (pct > 100) pct = 100;
    if (g_batteryValid) snprintf(line, sizeof(line), "BAT %.2fV %d%%", g_batteryVoltage, pct);
    else                snprintf(line, sizeof(line), "BAT --");
    oled->drawStr(0, 60, line);
  }
#else
  // 第 4 行: 有人 / 无人 / 看不清(12px 中文, 与标题同款)
  oled->setFont(u8g2_font_wqy12_t_gb2312);
  oled->drawUTF8(0, 62, presenceLabel());
#endif

  oled->sendBuffer();
}

// ============================================================================
// OLED 初始化: 自动探测 SDA/SCL 接法 + I2C 地址(0x3C/0x3D)
// ============================================================================
static bool i2cProbe(uint8_t addr) {
  Wire.beginTransmission(addr);
  return Wire.endTransmission() == 0;
}

bool oledInit() {
  // 候选地址: 配置值优先, 其次另一个常见地址
  const uint8_t addrs[2] = {
    (uint8_t)OLED_I2C_ADDR,
    (uint8_t)(OLED_I2C_ADDR == 0x3C ? 0x3D : 0x3C)
  };
  // 候选接法: 正常(SDA,SCL) / 接反(SDA<->SCL)
  struct { uint8_t sda, scl; U8G2 *inst; } tries[2] = {
    { PIN_OLED_SDA, PIN_OLED_SCL, &u8g2A },
    { PIN_OLED_SCL, PIN_OLED_SDA, &u8g2B },
  };

  for (int t = 0; t < 2; t++) {
    Wire.begin(tries[t].sda, tries[t].scl);
    delay(20);
    for (int i = 0; i < 2; i++) {
      if (!i2cProbe(addrs[i])) continue;
      g_oledAddr = addrs[i];
      oled = tries[t].inst;
      oled->setI2CAddress(g_oledAddr << 1);   // U8g2 用 8 位地址
      oled->begin();
      oled->setContrast(255);
      g_oledOk = true;
      LOGF("[OLED] OK addr=0x%02X SDA=%d SCL=%d%s\n",
           g_oledAddr, tries[t].sda, tries[t].scl,
           (t == 1) ? " (SDA/SCL swapped)" : "");
      return true;
    }
  }
  g_oledOk = false;
  LOGLN(F("[OLED] no device on 0x3C/0x3D — check wiring: GND/VDD(3V3)/SCK->42/SDA->2"));
  return false;
}

// GET /i2cscan —— 扫 I2C 总线, 远程诊断 OLED 是否被认到
void handleI2cScan() {
  String out = "I2C scan (configured SDA=" + String(PIN_OLED_SDA) +
               " SCL=" + String(PIN_OLED_SCL) + ")\n";
  out += "oled_ok=" + String(g_oledOk ? "true" : "false") +
         " addr=0x" + String(g_oledAddr, HEX) + "\n";
  for (uint8_t a = 0x08; a < 0x78; a++) {
    if (i2cProbe(a)) {
      out += "found 0x" + String(a, HEX) + "\n";
    }
  }
  out += "done\n";
  webServer.send(200, "text/plain; charset=utf-8", out);
}

// ============================================================================
// 风扇
// ============================================================================
void setupFan() {
  // core 3.x API: ledcAttachChannel。阶段二用 channel 4 / timer 1
  // (摄像头 XCLK 占用 LEDC channel 0 / timer 0, 冲突会导致摄像头发黑)
  ledcAttachChannel(PIN_FAN, FAN_PWM_FREQ, FAN_PWM_RESOLUTION, FAN_PWM_CHANNEL);
  setFan(FAN_SPEED_OFF);
}

void setFan(uint8_t speed) {
  ledcWrite(PIN_FAN, speed);  // 3.x 下第一参数是引脚而非通道
}

// ============================================================================
// 摄像头
// ============================================================================
bool initCameraOnce() {
  camera_config_t config = {};            // 必须零初始化
  config.ledc_channel = LEDC_CHANNEL_0;   // 摄像头占用 channel 0 (本板默认)
  config.ledc_timer   = LEDC_TIMER_0;
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
  config.xclk_freq_hz = (uint32_t)g_xclkMhz * 1000000UL;
  config.pixel_format = PIXFORMAT_JPEG;
  config.grab_mode    = CAMERA_GRAB_WHEN_EMPTY;

  if (psramFound()) {
    config.frame_size   = CAM_FRAME_SIZE;
    config.jpeg_quality = CAM_JPEG_QUALITY;
    config.fb_count     = 2;
    config.fb_location  = CAMERA_FB_IN_PSRAM;
  } else {
    // 无 PSRAM 时降级(本板 N8R8/N16R8 必有 PSRAM, 此分支仅兜底)
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

  // 抓一帧验证(排线接触不良时可能半初始化)
  camera_fb_t *probe = esp_camera_fb_get();
  if (!probe) {
    LOGLN(F("[CAM] init ok but capture failed, deinit and retry later"));
    esp_camera_deinit();
    return false;
  }
  esp_camera_fb_return(probe);

  LOGLN(F("[CAM] init ok"));

  // 图像方向校正: 用运行时保存的朝向(而不是写死), 避免不同模组装反
  applyRotation(g_rotation);

  sensor_t *s = esp_camera_sensor_get();
  if (s != NULL) {
    g_camPid = s->id.PID;
    // 色彩基线: 确保自动白平衡打开(偏色最常见的根因是 AWB/AWB增益被关掉)
    s->set_whitebal(s, 1);      // AWB 开
    s->set_awb_gain(s, 1);      // AWB 增益开
    s->set_exposure_ctrl(s, 1); // AEC 开
    s->set_gain_ctrl(s, 1);     // AGC 开
    s->set_brightness(s, 1);
    s->set_saturation(s, 0);
    s->set_lenc(s, 1);          // 镜头阴影校正
    s->set_bpc(s, 1);           // 坏点校正
    s->set_wpc(s, 1);           // 白点校正
    LOGF("[CAM] sensor PID=0x%04X, XCLK=%dMHz\n", s->id.PID, g_xclkMhz);
  }
  return true;
}

// ============================================================================
// 图像朝向控制(运行时可切换, NVS 持久化)
// ============================================================================
void applyRotation(uint8_t idx) {
  idx &= 0x03;
  sensor_t *s = esp_camera_sensor_get();
  if (s == NULL) return;
  // 1=vflip  2=hmirror  3=两者(180°)
  s->set_vflip(s,   (idx == 1 || idx == 3) ? 1 : 0);
  s->set_hmirror(s, (idx == 2 || idx == 3) ? 1 : 0);
  g_rotation = idx;
  LOGF("[CAM] rotation=%d (vflip=%d hmirror=%d)\n", idx,
       (idx == 1 || idx == 3) ? 1 : 0, (idx == 2 || idx == 3) ? 1 : 0);
}

void loadRotation() {
  g_prefs.begin("posture", false);
  // 默认 3 = vflip+hmirror(180°), 本板 OV2640 实测正确朝向
  g_rotation = g_prefs.getUChar("rot", 3);
  g_xclkMhz  = g_prefs.getUChar("xclk", 10);
  if (g_xclkMhz < 6 || g_xclkMhz > 27) g_xclkMhz = 10;
  g_prefs.end();
  LOGF("[CAM] loaded from NVS: rotation=%d xclk=%dMHz\n", g_rotation, g_xclkMhz);
}

// GET /rotate         -> 切换到下一种朝向(0->1->2->3->0)
// GET /rotate?n=0..3  -> 直接指定朝向
void handleRotate() {
  uint8_t idx;
  if (webServer.hasArg("n")) {
    idx = (uint8_t)(webServer.arg("n").toInt() & 0x03);
  } else {
    idx = (g_rotation + 1) & 0x03;
  }
  applyRotation(idx);
  g_prefs.begin("posture", false);
  g_prefs.putUChar("rot", idx);
  g_prefs.end();

  String msg = "rotation=" + String(idx) +
               " (0=normal 1=vflip 2=hmirror 3=180)\n";
  msg += "open /capture to check the image\n";
  webServer.send(200, "text/plain; charset=utf-8", msg);
}

// ============================================================================
// 摄像头参数在线调节: GET /cam?k=v&...
// 用于排查偏色(紫/红/绿)/(亮度/对比度)问题, 不写 NVS, 重启复位
// 支持键: wb(0-4) awb(0/1) awbg(0/1) expo(0/1) aec2(0/1) ae(-2..2)
//         gain(0/1) agc(0-3) bpc(0/1) wpc(0/1) rawgma(0/1) lenc(0/1)
//         effect(0-6) bright(-2..2) contrast(-2..2) sat(-2..2) sharp(-2..2) denoise(0-1)
// 例: /cam?wb=1  /cam?awb=1&awbg=1  /cam?lenc=0  /cam?sat=-2
// ============================================================================
void handleCam() {
  sensor_t *s = esp_camera_sensor_get();
  if (s == NULL) { webServer.send(500, "text/plain", "camera not ready"); return; }

  if (webServer.hasArg("wb"))       s->set_wb_mode(s, webServer.arg("wb").toInt());
  if (webServer.hasArg("awb"))      s->set_whitebal(s, webServer.arg("awb").toInt());
  if (webServer.hasArg("awbg"))     s->set_awb_gain(s, webServer.arg("awbg").toInt());
  if (webServer.hasArg("expo"))     s->set_exposure_ctrl(s, webServer.arg("expo").toInt());
  if (webServer.hasArg("aec2"))     s->set_aec2(s, webServer.arg("aec2").toInt());
  if (webServer.hasArg("ae"))       s->set_ae_level(s, webServer.arg("ae").toInt());
  if (webServer.hasArg("gain"))     s->set_gain_ctrl(s, webServer.arg("gain").toInt());
  if (webServer.hasArg("agc"))      s->set_gainceiling(s, (gainceiling_t)webServer.arg("agc").toInt());
  if (webServer.hasArg("bpc"))      s->set_bpc(s, webServer.arg("bpc").toInt());
  if (webServer.hasArg("wpc"))      s->set_wpc(s, webServer.arg("wpc").toInt());
  if (webServer.hasArg("rawgma"))   s->set_raw_gma(s, webServer.arg("rawgma").toInt());
  if (webServer.hasArg("lenc"))     s->set_lenc(s, webServer.arg("lenc").toInt());
  if (webServer.hasArg("effect"))   s->set_special_effect(s, webServer.arg("effect").toInt());
  if (webServer.hasArg("bright"))   s->set_brightness(s, webServer.arg("bright").toInt());
  if (webServer.hasArg("contrast")) s->set_contrast(s, webServer.arg("contrast").toInt());
  if (webServer.hasArg("sat"))      s->set_saturation(s, webServer.arg("sat").toInt());
  if (webServer.hasArg("sharp"))    s->set_sharpness(s, webServer.arg("sharp").toInt());
  if (webServer.hasArg("denoise"))  s->set_denoise(s, webServer.arg("denoise").toInt());

  String o = "sensor PID=0x" + String(g_camPid, HEX) + "\n";
  o += "wb_mode=" + String(s->status.wb_mode) + " whitebal=" + String(s->status.awb) +
       " awb_gain=" + String(s->status.awb_gain) + "\n";
  o += "expo=" + String(s->status.aec) + " aec2=" + String(s->status.aec2) +
       " ae_level=" + String(s->status.ae_level) + " gain=" + String(s->status.agc) +
       " gainceiling=" + String((int)s->status.gainceiling) + "\n";
  o += "bpc=" + String(s->status.bpc) + " wpc=" + String(s->status.wpc) +
       " raw_gma=" + String(s->status.raw_gma) + " lenc=" + String(s->status.lenc) + "\n";
  o += "effect=" + String(s->status.special_effect) + " bright=" + String(s->status.brightness) +
       " contrast=" + String(s->status.contrast) + " sat=" + String(s->status.saturation) +
       " sharp=" + String(s->status.sharpness) + " denoise=" + String(s->status.denoise) + "\n";
  webServer.send(200, "text/plain; charset=utf-8", o);
}

// ============================================================================
// XCLK 频率切换(部分模组 XCLK 不合适会偏色/撕裂): /xclk?mhz=10|20
// ============================================================================
void handleXclk() {
  if (!webServer.hasArg("mhz")) {
    webServer.send(200, "text/plain; charset=utf-8",
                   "xclk=" + String(g_xclkMhz) + "MHz\nusage: /xclk?mhz=10\n");
    return;
  }
  int mhz = webServer.arg("mhz").toInt();
  if (mhz < 6 || mhz > 27) { webServer.send(400, "text/plain", "mhz 6..27"); return; }

  g_xclkMhz = (uint8_t)mhz;
  g_prefs.begin("posture", false);
  g_prefs.putUChar("xclk", g_xclkMhz);
  g_prefs.end();

  // 改 XCLK 必须重初始化摄像头
  esp_camera_deinit();
  delay(200);
  g_camOk = initCameraOnce();
  if (g_camOk) g_lastUploadMs = millis();

  webServer.send(200, "text/plain; charset=utf-8",
                 "xclk=" + String(g_xclkMhz) + "MHz cam_ok=" + String(g_camOk ? "true" : "false") + "\n");
}

// ============================================================================
// WiFi
// ============================================================================
void setupWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);

  int added = 0;
  for (int i = 0; i < WIFI_NET_COUNT; i++) {
    if (WIFI_SSIDS[i] != NULL && strlen(WIFI_SSIDS[i]) > 0) {
      wifiMulti.addAP(WIFI_SSIDS[i], WIFI_PASSWORDS[i]);
      added++;
    }
  }
  LOGF("[WiFi] connecting, %d known network(s) ...\n", added);
  if (wifiMulti.run(WIFI_TIMEOUT_MS) == WL_CONNECTED) {
    LOGF("[WiFi] connected, IP: %s\n", WiFi.localIP().toString().c_str());
  } else {
    LOGLN(F("[WiFi] first connect timeout, will retry at runtime"));
  }
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

// ============================================================================
// 安静开关(拨动开关, 电平检测) —— 阶段二恢复
// ============================================================================
void updateQuietSwitch() {
#if BUTTON_ENABLED
  bool reading = (digitalRead(PIN_BUTTON) == BUTTON_ACTIVE_LEVEL);
  if (reading != g_quietActive) {
    g_quietActive = reading;
    if (g_quietActive) {
      LOGLN(F("[SW] quiet switch ON -> snooze"));
      if (g_state != STATE_SNOOZE) enterSnooze();
    } else {
      LOGLN(F("[SW] quiet switch OFF -> resume"));
      if (g_state == STATE_SNOOZE) enterNormal();
    }
  }
#endif
}

// ============================================================================
// 电池电量监测(阶段二新增)
// ============================================================================
void updateBattery() {
#if !BAT_ENABLED
  // USB 直供, 未装电池 → 不做 ADC 采样, 避免悬空误报
  g_batteryValid = false;
  g_batteryLow = false;
  return;
#else
  uint32_t now = millis();
  if (now - g_batLastCheckMs < BAT_CHECK_INTERVAL_MS) return;
  g_batLastCheckMs = now;

  int raw = analogRead(PIN_BAT_ADC);
  float v = (float)raw / BAT_ADC_RESOLUTION * BAT_ADC_VREF * BAT_DIVIDER_RATIO;
  g_batteryVoltage = v;
  // 分压未接时 ADC 悬空, 读数无意义 → 电压明显不合理就判定"未接电池检测"
  g_batteryValid = (v >= 2.50f && v <= 5.20f);
  g_batteryLow = g_batteryValid && (v < BAT_LOW_VOLTAGE);
  if (g_batteryLow) {
    LOGF("[BAT] low voltage: %.2fV\n", v);
  }
#endif
}

// ============================================================================
// 设备自检(阶段二新增 H4)
// ============================================================================
void runSelfTest() {
  LOGLN(F("[SELFTEST] start"));

  // 1. 摄像头
  g_selftest.cam = initCameraOnce();
  if (g_selftest.cam) g_camOk = true;
  LOGF("[SELFTEST] camera: %s\n", g_selftest.cam ? "OK" : "FAIL");

  // 2. 风扇通路(200ms 短脉冲, 观察是否转)
  setFan(FAN_SPEED_ON);
  delay(200);
  setFan(FAN_SPEED_OFF);
  g_selftest.fan = true;  // 软件通路正常(硬件是否转需人工观察)
  LOGLN(F("[SELFTEST] fan: pulse sent (200ms)"));

  // 3. LED 通路(红绿各闪一次)
  setLedColor(LED_RED);   delay(150);
  setLedColor(LED_GREEN); delay(150);
  setLedColor(LED_OFF);
  g_selftest.led = true;
  LOGLN(F("[SELFTEST] led: red+green blinked"));

  // 4. WiFi
  g_selftest.wifi = (WiFi.status() == WL_CONNECTED);
  LOGF("[SELFTEST] wifi: %s\n", g_selftest.wifi ? "OK" : "FAIL");

  g_selftest.ran = true;
  LOGLN(F("[SELFTEST] done"));
}

// ============================================================================
// 上传与处理
// ============================================================================
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
    g_uploadFailStreak++;
    return false;
  }

  String payload = http.getString();
  http.end();
  g_uploadFailStreak = 0;

  JsonDocument doc;  // ArduinoJson v7
  DeserializationError err = deserializeJson(doc, payload);
  if (err) {
    LOGF("[JSON] parse failed: %s\n", err.c_str());
    return false;
  }

  const char *action = doc["action"] | "none";
  LOGF("[HTTP] resp: %s\n", payload.c_str());

  // ---- 解析 v2 字段(有人/无人/看不清) ----
  g_lastDetectOkMs = millis();
  g_hasDetectOk = true;
  g_personFlag = doc["person"] | false;
  const char *scene = doc["scene"] | "";
  const char *disp  = doc["display"] | "";
  strncpy(g_sceneBuf, scene, sizeof(g_sceneBuf) - 1);
  g_sceneBuf[sizeof(g_sceneBuf) - 1] = '\0';
  strncpy(g_displayBuf, disp, sizeof(g_displayBuf) - 1);
  g_displayBuf[sizeof(g_displayBuf) - 1] = '\0';

  if (strcmp(scene, "person") == 0) {
    // 看到人立即生效, 不做去抖
    g_presence = PRESENCE_PERSON;
    g_emptyStreak = 0;
    g_occlStreak  = 0;
  } else if (strcmp(scene, "empty") == 0) {
    g_occlStreak = 0;
    if (g_emptyStreak < 255) g_emptyStreak++;
    if (g_emptyStreak >= PRESENCE_DEBOUNCE) g_presence = PRESENCE_EMPTY;
  } else if (strcmp(scene, "occluded") == 0) {
    g_emptyStreak = 0;
    if (g_occlStreak < 255) g_occlStreak++;
    if (g_occlStreak >= PRESENCE_DEBOUNCE) g_presence = PRESENCE_OCCLUDED;
  } else {
    // 老服务器没有 scene 字段 → 未知, 不猜"无人"
    g_presence = PRESENCE_UNKNOWN;
    g_emptyStreak = 0;
    g_occlStreak  = 0;
  }

  if (strcmp(action, "blow") == 0) {
    enterBlowing();
  }
  return true;
}

// ============================================================================
// 状态机
// ============================================================================
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
        // 若安静开关仍拨在"安静"位, 不自动退出(由开关电平决定)
        if (!g_quietActive) {
          LOGLN(F("[SNOOZE] over, resume"));
          enterNormal();
        }
      }
      break;
  }
}

// ============================================================================
// 调试用 Web 接口
// ============================================================================
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

void handleStatus() {
  String s = "{";
  s += "\"cam_ok\":" + String(g_camOk ? "true" : "false");
  s += ",\"wifi\":" + String(WiFi.status() == WL_CONNECTED ? "true" : "false");
  s += ",\"ip\":\"" + WiFi.localIP().toString() + "\"";
  s += ",\"state\":" + String((int)g_state);
  s += ",\"uptime_s\":" + String(millis() / 1000);
  s += ",\"quiet_switch\":" + String(g_quietActive ? "true" : "false");
  s += ",\"battery_v\":" + String(g_batteryVoltage, 2);
  s += ",\"battery_low\":" + String(g_batteryLow ? "true" : "false");
  s += ",\"upload_fail_streak\":" + String(g_uploadFailStreak);
  s += ",\"rotation\":" + String(g_rotation);
  s += ",\"sensor_pid\":" + String(g_camPid);
  s += ",\"xclk_mhz\":" + String(g_xclkMhz);
  s += ",\"battery_valid\":" + String(g_batteryValid ? "true" : "false");
  s += ",\"presence\":\"" + String(presenceLabel()) + "\"";
  s += ",\"scene\":\"" + String(g_sceneBuf) + "\"";
  s += ",\"person\":" + String(g_personFlag ? "true" : "false");
  s += ",\"display\":\"" + String(g_displayBuf) + "\"";
  s += ",\"last_detect_age_s\":" +
       (g_hasDetectOk ? String((millis() - g_lastDetectOkMs) / 1000) : String(-1));
  s += ",\"oled_ok\":" + String(g_oledOk ? "true" : "false");
  s += ",\"oled_addr\":\"0x" + String(g_oledAddr, HEX) + "\"";
  s += ",\"selftest\":{";
  s += "\"ran\":" + String(g_selftest.ran ? "true" : "false");
  s += ",\"cam\":" + String(g_selftest.cam ? "true" : "false");
  s += ",\"fan\":" + String(g_selftest.fan ? "true" : "false");
  s += ",\"led\":" + String(g_selftest.led ? "true" : "false");
  s += ",\"wifi\":" + String(g_selftest.wifi ? "true" : "false");
  s += "}";
  s += "}";
  webServer.send(200, "application/json", s);
}

// ============================================================================
// 状态切换
// ============================================================================
void enterBlowing() {
  if (g_state == STATE_SNOOZE) return;
  LOGLN(F("[STATE] -> BLOWING (5s)"));
  g_state       = STATE_BLOWING;
  g_blowStartMs = millis();
  setFan(FAN_SPEED_ON);
}

void enterSnooze() {
  LOGLN(F("[STATE] -> SNOOZE"));
  g_state         = STATE_SNOOZE;
  g_snoozeStartMs = millis();
  setFan(FAN_SPEED_OFF);
}

void enterNormal() {
  LOGLN(F("[STATE] -> NORMAL"));
  g_state        = STATE_NORMAL;
  setFan(FAN_SPEED_OFF);
  g_lastUploadMs = millis();
}

// ============================================================================
// setup / loop
// ============================================================================
void setup() {
  Serial.begin(115200);
  Serial.setDebugOutput(false);
  delay(200);
  LOGLN();
  LOGLN(F("[BOOT] posture monitor stage 2 (ESP32-S3-CAM)"));

  pinMode(PIN_LED_RED,   OUTPUT);
  pinMode(PIN_LED_GREEN, OUTPUT);
  setLedColor(LED_OFF);
#if BUTTON_ENABLED
  pinMode(PIN_BUTTON, INPUT_PULLUP);
#endif
  pinMode(PIN_BAT_ADC, INPUT);

  // 0.96 OLED (I2C) —— 自动探测接线与地址(见 oledInit)
  if (oledInit()) {
    oled->clearBuffer();
    oled->setFont(u8g2_font_wqy12_t_gb2312);
    oled->drawUTF8(0, 12, "学习坐姿监测");
    oled->setFont(u8g2_font_6x10_tf);
    oled->drawStr(0, 30, "BOOTING...");
    oled->sendBuffer();
  }

  setupFan();
  setupWiFi();

  // 读回上次保存的图像朝向(必须在校准摄像头前加载)
  loadRotation();

  // 开机自检(阶段二 H4) —— 内部会调用 initCameraOnce(), 其中应用 g_rotation
  runSelfTest();

  webServer.on("/capture", HTTP_GET, handleCapture);
  webServer.on("/status",  HTTP_GET, handleStatus);
  webServer.on("/rotate",  HTTP_GET, handleRotate);
  webServer.on("/i2cscan", HTTP_GET, handleI2cScan);
  webServer.on("/cam",     HTTP_GET, handleCam);
  webServer.on("/xclk",    HTTP_GET, handleXclk);
  webServer.begin();
  LOGF("[WEB] status: http://%s/status  rotate: http://%s/rotate\n",
       WiFi.localIP().toString().c_str(), WiFi.localIP().toString().c_str());

  g_lastUploadMs = millis();
  LOGLN(F("[BOOT] ready, state=NORMAL"));
}

void loop() {
#if BUTTON_ENABLED
  updateQuietSwitch();
#endif

  // 摄像头不可用: 定期重试
  if (!g_camOk && millis() - g_camRetryMs >= CAM_RETRY_INTERVAL_MS) {
    g_camRetryMs = millis();
    g_camOk = initCameraOnce();
    if (g_camOk) {
      LOGLN(F("[CAM] recovered, resume monitoring"));
      g_lastUploadMs = millis();
    }
  }

  updateBattery();
  refreshStatusLed();
  refreshOled();
  tickStateMachine();
  webServer.handleClient();
}
