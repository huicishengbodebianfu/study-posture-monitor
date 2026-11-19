#ifndef CONFIG_H
#define CONFIG_H

// WiFi：只支持一个网络，填 2.4GHz 的（ESP32 不支持 5GHz）
static const char *WIFI_SSID = "YOUR_WIFI_SSID";
static const char *WIFI_PASS = "YOUR_WIFI_PASSWORD_2";

// 若想再挂一个备用网络，把下面两行改成宏数组版（默认不启用）
// static const char *WIFI_SSID2 = "YOUR_PHONE_HOTSPOT";
// static const char *WIFI_PASS2 = "YOUR_WIFI_PASSWORD";

#endif
