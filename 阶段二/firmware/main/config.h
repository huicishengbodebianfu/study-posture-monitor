#ifndef CONFIG_H
#define CONFIG_H

// ============================================================================
// 阶段二固件配置 —— ESP32-S3-CAM
// 编写日期: 2026-11-01
//
// 相比阶段一(ESP32-CAM)的关键变化:
//   1. 主控换 ESP32-S3-WROOM-1 (N8R8/N16R8, 必须带 PSRAM)
//   2. GPIO0 不再是摄像头 XCLK -> 可安全用于安静开关(但本设计改到 GPIO41)
//   3. 恢复安静开关(拨动开关, 电平检测), 新增双色状态灯, 新增电池电量 ADC 监测
//   4. 风扇 PWM 改用 LEDC channel 4 (XCLK 占用 channel 0/timer 0)
// ============================================================================

// ----------------------------------------------------------------------------
// 板型选择 —— 只影响摄像头引脚(在 main.ino 顶部由 #if 切换)
//
//   默认(下面那行保持注释) = CAMERA_MODEL_ESP32S3_EYE = Freenove FNK0085/FNK0086(无屏)
//   有 LCD 彩色屏的板 = 取消注释下面那行
//
// 判定: 板子上有没有一块小 LCD 彩色屏?
//   - 没屏 → 保持默认(不用改)
//   - 有屏 → 把下面这行前面的 // 去掉
// ----------------------------------------------------------------------------
// #define CAMERA_MODEL_ESP32S3_CAM_LCD

// WiFi（可配置多个网络；开机自动连接信号最好的一个）
// 用法：按顺序填写 SSID/密码；不用的槽位保留空字符串 "" 即自动跳过
#define WIFI_TIMEOUT_MS 15000
#define WIFI_NET_COUNT  3

static const char* WIFI_SSIDS[WIFI_NET_COUNT] = {
  "YOUR_WIFI_SSID",         // 主 WiFi（必须 2.4GHz）
  "YOUR_PHONE_HOTSPOT",     // 备用热点
  ""                        // 备用（不用就留空）
};
static const char* WIFI_PASSWORDS[WIFI_NET_COUNT] = {
  "YOUR_WIFI_PASSWORD",
  "YOUR_WIFI_PASSWORD_2",
  ""
};

// 云端服务器，例：http://YOUR_SERVER_IP:8000/detect
#define SERVER_URL      "http://YOUR_SERVER_IP:8000/detect"                    // 云端检测接口
#define DEVICE_KEY      "CHANGE_ME_DEVICE_KEY"                    // 与云端 .env 一致，部署前务必改成随机值

// ============================================================================
// 外设引脚（ESP32-S3-CAM 空闲 GPIO: 1, 2, 14, 21, 41, 42, 47）
//
// 严禁使用(板载资源/总线, 接了会坏):
//   GPIO35/36/37 = 八进制 PSRAM 总线(SPIDQS/SPIIO6/SPIIO7)
//   GPIO38/39/40 = SD 卡槽(CMD/CLK/D0)
//   GPIO43/44    = UART0 TX/RX (CH343 桥接)
//   GPIO19/20    = 原生 USB D-/D+
//   GPIO45/46    = strapping(VDD_SPI 电压选择/启动模式)
//   GPIO0        = BOOT 按钮(strapping, 上电弱上拉)
//   GPIO2        = 板载 LED(高电平亮) —— 可作调试, 但别接大负载
//   GPIO48       = WS2812 数据脚
// ============================================================================

#define PIN_FAN         21    // 风扇 PWM(经 S8050 三极管驱动) —— LEDC channel 4
#define PIN_LED_RED     14    // 红色 LED(双色灯红通道, 串 220Ω)
#define PIN_LED_GREEN   2     // 绿色 LED —— 板载 LED 脚(高电平亮), 也可接双色灯绿通道
#define PIN_BUTTON      47    // 安静开关(拨动开关, 另一端接 GND, 内部上拉)
#define PIN_BAT_ADC     1     // 电池电量 ADC(ADC1_CH0, 分压后接入)  [阶段二新增]

// 0.96 OLED (I2C, SSD1306, 128x64) —— 扩展板 4 线接口 GND/VDD/SCK/SDA
// ⚠️ 必须显式指定引脚: Arduino 默认 Wire 是 GPIO8(SDA)/GPIO9(SCL), 但这两脚被摄像头占用!
// 【2026-11-08 万用表实测】扩展板 OLED 座子: SCK -> GPIO41, SDA -> GPIO42
#define PIN_OLED_SCL    41    // I2C 时钟 -> OLED SCK
#define PIN_OLED_SDA    42    // I2C 数据 -> OLED SDA
#define OLED_I2C_ADDR   0x3C  // SSD1306 常见地址 0x3C (固件会自动再试 0x3D)

// 安静开关(拨动开关, 电平检测而非边沿)
// 阶段一: GPIO0 与摄像头 XCLK 冲突, 被迫 BUTTON_ENABLED=0
// 阶段二: 用 GPIO47(2026-11-08 因 GPIO41 被 OLED 占用而从 41 改到 47)
// 注意: 不要用 GPIO2 做输入 —— 板载 LED 经电阻接地, 内部弱上拉拉不动, 读数不稳
#define BUTTON_ENABLED  1
#define BUTTON_ACTIVE_LEVEL  LOW   // 拨到"安静"位 = 开关闭合到 GND = LOW

// 行为参数
#define UPLOAD_INTERVAL_MS   10000UL
#define BLOW_DURATION_MS     5000UL
#define SNOOZE_DURATION_MS   300000UL
#define HTTP_TIMEOUT_MS      8000UL
#define CAM_RETRY_INTERVAL_MS 30000UL   // 摄像头初始化失败后的重试间隔（毫秒）

// 风扇 PWM —— 阶段二: 用 channel 4 (摄像头 XCLK 占用 LEDC channel 0 / timer 0)
#define FAN_PWM_CHANNEL      4
#define FAN_PWM_TIMER        1
#define FAN_PWM_FREQ         5000
#define FAN_PWM_RESOLUTION   8
#define FAN_SPEED_ON         255
#define FAN_SPEED_OFF        0

// 电池电量监测（阶段二新增 H4/H5）
// ⚠️ 当前 USB 直供、不装电池 → BAT_ENABLED=0 关闭(否则 ADC 悬空会误报低电)
#define BAT_ENABLED          0              // 1=启用电池监测, 0=关闭(USB直供)
// 18650 满电 4.2V, 分压 10k(上)+10k(下)=1:1, 满电读出约 2.1V(安全, <3.3V)
#define BAT_ADC_RESOLUTION   4095.0f    // 12-bit ADC
#define BAT_ADC_VREF         3.3f       // ADC 参考电压
#define BAT_DIVIDER_RATIO    2.0f       // 1:1 分压, 实际电压 = 采样 * 2
#define BAT_FULL_VOLTAGE     4.20f      // 满电
#define BAT_LOW_VOLTAGE      3.30f      // 低电量告警阈值
#define BAT_CHECK_INTERVAL_MS 30000UL   // 每 30 秒采样一次

// 有人/无人显示(OLED 第4行) —— 取自 /detect v2 的 scene 字段
#define PRESENCE_DEBOUNCE    2          // empty/occluded 需连续 N 帧才切换
#define OFFLINE_TIMEOUT_MS   30000UL    // 超过该时长无成功响应 → 显示"离线"

// 摄像头
#define CAM_FRAME_SIZE       FRAMESIZE_VGA   // 与阶段一一致(640x480)
#define CAM_JPEG_QUALITY     12
#define CAM_XCLK_FREQ        10000000        // 10MHz —— board-hardware 建议, 防 FPC 撕裂

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
