#ifndef CONFIG_H
#define CONFIG_H

// ================= 板型选择（必选其一）=================
// Freenove ESP32-S3-WROOM CAM、Espressif ESP32-S3-EYE、多数"S3-CAM"克隆板
#define BOARD_S3_FREENOVE   1
#define BOARD_NAME          "s3-freenove"

// Seeed XIAO ESP32S3 Sense（带摄像头扩展板）
// 换成这一组：注释掉上面两行，取消下面两行注释
// #define BOARD_S3_XIAO_SENSE 1
// #define BOARD_NAME          "s3-xiao-sense"

// ================= WiFi（只支持 2.4GHz）=================
static const char *WIFI_SSID = "YOUR_WIFI_SSID";
static const char *WIFI_PASS = "YOUR_WIFI_PASSWORD_2";

#endif
