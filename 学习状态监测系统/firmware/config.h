#ifndef CONFIG_H
#define CONFIG_H

// WiFi（可配置多个网络；开机自动连接信号最好的一个）
// 用法：按顺序填写 SSID/密码；不用的槽位保留空字符串 "" 即自动跳过
#define WIFI_TIMEOUT_MS 15000
#define WIFI_NET_COUNT  3

static const char* WIFI_SSIDS[WIFI_NET_COUNT] = {
  "YOUR_PHONE_HOTSPOT",    // 手机热点（注意必须开 2.4GHz）
  "YOUR_WIFI_SSID",        // 主 WiFi
  ""                     // 备用（不用就留空）
};
static const char* WIFI_PASSWORDS[WIFI_NET_COUNT] = {
  "YOUR_WIFI_PASSWORD",
  "YOUR_WIFI_PASSWORD_2",
  ""
};

// 云端服务器，例：http://123.45.67.89:8000/detect
#define SERVER_URL      "http://YOUR_SERVER_IP:8000/detect"                 // 已部署的云端检测接口
#define DEVICE_KEY      "CHANGE_ME_DEVICE_KEY"                  // 与云端 .env 一致

// 外设引脚（AI Thinker ESP32-CAM）
#define PIN_FAN         13    // 风扇 PWM(经 S8050 三极管/MOS 管)
#define PIN_LED_RED     14    // 红色 LED
// 免打扰按钮开关
// ⚠️ 本板没有 BOOT 按键；且 GPIO0 同时是摄像头 XCLK（20MHz 时钟输出），
//    若把 GPIO0 当按键读取，会误触发免打扰（表现为上传中断 5 分钟，实测已证实）。
//    因此默认关闭。日后如需外接按键：把 PIN_BUTTON 改到空闲脚（如 15），再把 BUTTON_ENABLED 改成 1。
#define BUTTON_ENABLED  0
#define PIN_BUTTON      0     // 仅 BUTTON_ENABLED=1 时使用；不要用 0（与摄像头 XCLK 冲突）

// 行为参数
#define UPLOAD_INTERVAL_MS   10000UL
#define BLOW_DURATION_MS     5000UL
#define SNOOZE_DURATION_MS   300000UL
#define BUTTON_DEBOUNCE_MS   50UL
#define HTTP_TIMEOUT_MS      8000UL
#define CAM_RETRY_INTERVAL_MS 30000UL   // 摄像头初始化失败后的重试间隔（毫秒）

// 风扇 PWM
#define FAN_PWM_CHANNEL      0
#define FAN_PWM_FREQ         5000
#define FAN_PWM_RESOLUTION   8
#define FAN_SPEED_ON         255
#define FAN_SPEED_OFF        0

// 摄像头
#define CAM_FRAME_SIZE       FRAMESIZE_VGA
#define CAM_JPEG_QUALITY     12
#define CAM_XCLK_FREQ        20000000

// 调试日志
#define DEBUG_SERIAL         1
#if DEBUG_SERIAL
  #define LOGF(...) Serial.printf(__VA_ARGS__)
  #define LOGLN(x)  Serial.println(x)
#else
  #define LOGF(...) do {} while (0)
  #define LOGLN(x)  do {} while (0)
#endif

#endif
