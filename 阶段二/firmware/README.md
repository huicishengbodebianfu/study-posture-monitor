# 阶段二固件 · 烧录说明

> ⚠️ **本文 §3.6 的 OLED 引脚是旧稿（GPIO42/2）**；以 `config.h` 的 **SCL=GPIO41 / SDA=GPIO42** 为准（2026-11-08 万用表实测）。
> 接线请以 `阶段二/硬件资料/接线图-阶段二-定稿.md` 为准。

> 目标板：**ESP32-S3-CAM**（Freenove FNK0085/FNK0086，摄像头 = CAMERA_MODEL_ESP32S3_EYE）
> 相比阶段一：主控从 ESP32-CAM 换到 ESP32-S3-WROOM-1，恢复安静开关，新增双色状态灯 + 电池电量监测 + 设备自检。

---

## 0. 硬件资料来源（已下载到 `阶段二/硬件资料/`）

| 文件 | 用途 |
|---|---|
| `camera_pins.h` | 摄像头引脚定义（CAMERA_MODEL_ESP32S3_EYE） |
| `ESP32-S3CAM原理图.pdf` | 原理图（3 页，含 BOM） |
| `ESP32S3CAM-Pin.jpg` | 40 脚排针引脚图 |
| `ESP32-S3CAM.pdf` / `Start Here.pdf` | 板卡说明书 |
| `C_Tutorial.pdf` | Freenove C 教程 |
| `esp32s3cam尺寸图.png` | 板卡尺寸图 |

**板卡关键事实**（已核验原理图 + 权威社区文档）：

| 项 | 值 |
|---|---|
| 主控 | ESP32-S3-WROOM-1（**N8R8 或 N16R8，必须带 PSRAM**） |
| 摄像头 | 24pin FPC，OV2640 / OV3660 |
| 摄像头引脚 | `CAMERA_MODEL_ESP32S3_EYE`（见 main.ino 顶部） |
| USB | 两个 USB-C：CH343 串口桥 + 原生 USB |
| 串口 | UART0 = GPIO43(TX) / GPIO44(RX)，接 CH343 |
| WS2812 | GPIO48 |
| 板载 LED | GPIO2（高电平亮） |
| BOOT 按钮 | GPIO0 |
| SD 卡 | GPIO38(CMD) / 39(CLK) / 40(D0) |

---

## 1. 引脚分配（阶段二）

### 摄像头（不可改）

| 信号 | GPIO |
|---|---|
| XCLK | 15 |
| SIOD (SCCB SDA) | 4 |
| SIOC (SCCB SCL) | 5 |
| D0..D7 (Y2..Y9) | 11, 9, 8, 10, 12, 18, 17, 16 |
| VSYNC | 6 |
| HREF | 7 |
| PCLK | 13 |
| PWDN / RESET | -1（本板未接线） |

### 外设（阶段二新增/变更）

| 功能 | GPIO | 说明 |
|---|---|---|
| 风扇 PWM | **21** | LEDC **channel 4 / timer 1**（XCLK 占用 channel 0） |
| 红灯（双色灯红） | **14** | 串 220Ω |
| 绿灯（双色灯绿） | **47** | 串 220Ω，阶段二新增 |
| 安静开关（拨动） | **41** | 电平检测，另一端接 GND，内部上拉 |
| 电池电量 ADC | **1** | ADC1_CH0，10k+10k 分压 |

### ⛔ 严禁使用的引脚

| GPIO | 原因 |
|---|---|
| 35 / 36 / 37 | 八进制 PSRAM 总线（SPIDQS/SPIIO6/SPIIO7），接了会 LoadProhibited |
| 38 / 39 / 40 | SD 卡槽 |
| 43 / 44 | UART0 TX/RX |
| 19 / 20 | 原生 USB D-/D+ |
| 45 / 46 | strapping（VDD_SPI 电压 / 启动模式） |
| 0 | BOOT 按钮 + strapping |
| 2 | 板载 LED（可作调试，勿接大负载） |
| 48 | WS2812 |

---

## 2. 编译

### 板卡参数（务必照抄，否则 PSRAM 不生效 → 摄像头 0x101 内存错误）

> ✅ 已按你的板子实测信息定稿（2026-11-08）：
> ESP32-S3 QFN56 rev v0.2，**8MB PSRAM(3.3V) + 16MB quad Flash(0x68/0x4018)**，晶振 40MHz，原生 USB-Serial/JTAG。
> 即 **ESP32-S3-WROOM-1-N16R8**。

```powershell
$cli = '__tools\arduino-cli\arduino-cli.exe'
$sketch = '阶段二\firmware\main'
$fqbn = 'esp32:esp32:esp32s3:PSRAM=opi,FlashSize=16M,FlashMode=qio,PartitionScheme=app3M_fat9M_16MB,CDCOnBoot=cdc'
& $cli compile --fqbn $fqbn --build-property "build.extra_flags=-DBOARD_HAS_PSRAM" $sketch
```

四个关键点（对照板子实测信息）：
1. **`PSRAM=opi`** —— 你的板是 8MB 八进制(OPI) PSRAM；默认 `disabled` 会导致 `psramFound()` 返回 false、摄像头 `0x101 ESP_ERR_NO_MEM`。
2. **`-DBOARD_HAS_PSRAM`** —— 不定义则 Arduino core 跳过 PSRAM 初始化。
3. **`FlashSize=16M` + `FlashMode=qio`** —— 你的板是 **16MB quad Flash**（之前误用 8M，已纠正）。
4. **`CDCOnBoot=cdc`** —— 你的串口是原生 USB-Serial/JTAG（COM6，VID_303A PID_1001），开 CDC 后 Serial 走原生 USB 口。

### 烧录

你的板子两个 USB-C 口功能不同（已按板子实测信息确认）：

| 口 | 枚举 | 用途 |
|---|---|---|
| **原生 USB-Serial/JTAG**（你的 COM6，VID_303A PID_1001） | `COM6` | ✅ 用这个烧录 + 看串口 |
| CH343 串口桥（VID_1A86） | 另一个 COM | 备用 |

烧录命令（`<COM>` 换成 `COM6`，`arduino-cli board list` 查）：

```powershell
& $cli upload -p COM6 --fqbn $fqbn --build-property "build.extra_flags=-DBOARD_HAS_PSRAM" $sketch
```

> 串口监视器看日志：`& $cli monitor -p COM6 --config baudrate=115200`
```

---

## 3. 阶段二新增功能说明

### 3.1 安静开关（恢复）

- 阶段一：GPIO0 与摄像头 XCLK 冲突，被迫 `BUTTON_ENABLED=0`。
- 阶段二：改用 **GPIO41 拨动开关**，电平检测（拨到安静位 = LOW = 进入 SNOOZE，拨回 = 恢复 NORMAL）。
- 与阶段一按钮的差异：这是**电平型**（持续拨着就持续免打扰），不是"按一下 5 分钟"。

### 3.2 双色状态灯（绿/黄/红）

| 状态 | 颜色 |
|---|---|
| 正常 | 绿常亮 |
| 吹风提醒 | 红闪烁（1Hz） |
| 免打扰/安静 | 红常亮 |
| 摄像头故障 / 低电量 / 连续 3 次上传失败 | 黄常亮 |

### 3.3 电池电量监测

- GPIO1（ADC1_CH0）经 10k+10k 分压读 18650 电压。
- 满电 4.2V → 采样约 2.1V；低于 3.3V 判定低电量，黄灯告警 + `/status` 报 `battery_low`。

### 3.4 设备自检（POST）

上电后自动执行：摄像头初始化 → 风扇 200ms 脉冲 → 红绿 LED 各闪一次 → WiFi 检查。结果暴露在 `/status` 的 `selftest` 字段。

### 3.5 /status 扩展字段

```json
{
  "cam_ok": true, "wifi": true, "ip": "192.168.1.100", "state": 0,
  "uptime_s": 1234, "quiet_switch": false,
  "battery_v": 4.08, "battery_low": false,
  "upload_fail_streak": 0,
  "selftest": {"ran": true, "cam": true, "fan": true, "led": true, "wifi": true}
}
```

### 3.6 OLED 显示（0.96寸，中文）

**接线**（扩展板 4 线接口 → ESP32-S3-CAM）：

| OLED 接口 | 接到板子 |
|---|---|
| GND | GND |
| VDD | 3V3 |
| SCK（=SCL） | **GPIO42** |
| SDA | **GPIO2**（板载 LED 脚，会随数据微闪，无害） |

> ⚠️ **必须显式指定 I2C 引脚**：Arduino 默认 `Wire` 用 GPIO8(SDA)/GPIO9(SCL)，但这两脚被摄像头占用。固件里已写 `Wire.begin(PIN_OLED_SDA, PIN_OLED_SCL)` 显式指定 GPIO2/42，**不要用默认 `Wire.begin()`**。

**屏幕布局**（128×64，每 1 秒刷新）：

```
┌─────────────────────────┐
│ 学习坐姿监测             │  ← 标题（12×12 中文字体）
│ 正常监测                 │  ← 状态大字（16×16 中文字体）
│ WiFi 192.168.1.100      │  ← 网络 + IP
│ BAT 4.08V 82%           │  ← 电量 + 百分比
└─────────────────────────┘
```

| 显示 | 含义 |
|---|---|
| 正常监测 | 正常运行 |
| 吹风提醒 | 正在吹风（红闪） |
| 免打扰中 | 安静开关/免打扰 |
| 摄像头故障 | cam_ok=false |
| 低电量/异常 | 低电量或连续 3 次上传失败 |

**中文字体**：`u8g2_font_wqy12_t_gb2312`（标题）+ `u8g2_font_wqy16_t_gb2312`（状态大字），用 `drawUTF8()` 绘制。源码文件必须保持 UTF-8 编码。

**I2C 地址**：0x3C / 0x3D **自动探测**；SDA/SCL 接反也**自动纠正**（固件建了两个 U8g2 实例分别对应两种接法）。

> ⚠️ **致命坑（已修复）**：`U8G2_..._HW_I2C` 构造函数若**不传 clock/data 引脚**，U8g2 初始化时会调用无参 `Wire.begin()`，I2C 被拉回默认 **GPIO8/9 = 摄像头 D2/D1**，导致 OLED 黑屏。
> 正确写法（第三个参数是 clock，第四个是 data）：
> ```cpp
> U8G2_SSD1306_128X64_NONAME_F_HW_I2C u8g2A(U8G2_R0, U8X8_PIN_NONE, PIN_OLED_SCL, PIN_OLED_SDA);
> ```

**诊断接口** `GET /i2cscan`：扫描 0x08~0x77，返回命中的地址、`oled_ok`、当前地址。用于远程判断"屏有没有被认到"。

**OLED 不亮排查顺序**：`/i2cscan` 是否 `found 0x3C/0x3D` → 没有则查接线（GND/VDD=3V3/SCK→42/SDA→2）→ 再查扩展板 OLED 座子是否真的引到了 GPIO42/2。

**flash 占用**：加中文显示后程序约 1.75MB（55%），16MB 分区下余量充足。

### 3.7 图像朝向（运行时可切换 + 掉电保存）

不同批次/厂家的摄像头模组安装方向不一致（有的正装、有的倒装 180°），**写死翻转容易反**。因此固件把朝向做成运行时可选：

| 编号 | 效果 | 对应寄存器 |
|---|---|---|
| 0 | 不翻转（默认） | vflip=0, hmirror=0 |
| 1 | 上下翻转 | vflip=1, hmirror=0 |
| 2 | 左右镜像 | vflip=0, hmirror=1 |
| 3 | 180° 旋转 | vflip=1, hmirror=1 |

**接口**：

- `GET /rotate` —— 切到下一种朝向（0→1→2→3→0），立即生效并写入 NVS
- `GET /rotate?n=0..3` —— 直接指定
- `GET /capture` —— 实时抓一帧看画面，用来判断方向
- `GET /status` —— 返回里含 `"rotation"` 字段

**实现要点**：

- `Preferences g_prefs`（NVS，key = `rot`，命名空间 `posture`）保存，重启后由 `loadRotation()` 读回
- `loadRotation()` 必须在摄像头初始化之前调用（`setup()` 里排在 `runSelfTest()` 之前）
- `applyRotation()` 在 `initCameraOnce()` 末尾调用，对任意传感器通用（不再按 PID 分支）
- 固件启动日志打印 `[CAM] rotation=N (vflip=.. hmirror=..)` 与 `[CAM] sensor PID=0x....`

**为什么必须调对**：图像倒置时云端 YOLOv8-pose 找不到人体关键点，`head_height`/`shoulder_level` 失效 → 风扇永远不吹，等于核心功能失效。

### 3.8 OLED 第4行：有人 / 无人 / 看不清（对接 `/detect` v2）

USB 直供、不装电池，`BAT_ENABLED=0` 关闭电量显示，第 4 行改为**场景显示**。

**数据源**：`/detect` v2 的 `scene` 字段（**不是** `person`）

| scene | 第4行 | 去抖 |
|---|---|---|
| `person` | 有人 | 立即 |
| `empty` | 无人 | 连续 `PRESENCE_DEBOUNCE`(2) 帧 |
| `occluded` | 看不清 | 连续 2 帧 |
| 字段缺失 | 未知 | — |
| 30 秒无成功响应 | 离线 | — |
| 上电未收到首帧 | 检测中 | — |
| `cam_ok=false` | — | — |

**为什么不用 `person` 布尔**：`person=false` 同时包含"空座位"和"镜头被挡/太暗"，只用 person 会把"看不清"误显示成"无人"。必须用 `scene`。

**实现**：

- `uploadAndHandle()` 解析 `v`/`person`/`scene`/`display`，维护 `g_presence` + 两个连续帧计数器
- `presenceLabel()` 统一输出文案（含离线/检测中/摄像头故障判断）
- `/status` 新增 `presence`、`scene`、`person`、`display`、`last_detect_age_s`
- `stateLabel()` 去掉 batteryLow，上传失败文案改为**网络异常**
- 黄灯条件改为：摄像头故障 / 上传连续失败 ≥3（不再有低电量）

**配置项**（`config.h`）：`BAT_ENABLED 0`、`PRESENCE_DEBOUNCE 2`、`OFFLINE_TIMEOUT_MS 30000`

---

## 4. 与阶段一差异速查

| 项 | 阶段一（ESP32-CAM） | 阶段二（ESP32-S3-CAM） |
|---|---|---|
| 摄像头引脚 | AI Thinker（XCLK=GPIO0） | ESP32S3_EYE（XCLK=GPIO15） |
| 风扇 GPIO / LEDC | 13 / channel 0 | 21 / channel 4 |
| 红灯 GPIO | 14 | 14 |
| 绿灯 | 无 | 47 |
| 安静开关 | 禁用（GPIO0 冲突） | 恢复（GPIO41 拨动） |
| 电池监测 | 无 | GPIO1 ADC |
| 状态灯 | 单红灯 | 绿/黄/红双色 |
| 自检 | 无 | 开机 POST + /status 扩展 |
| OLED 显示 | 无 | 0.96寸中文（GPIO42/2 I2C） |
| 图像朝向 | 写死 vflip（仅 OV3660） | 运行时 4 档可切换 + NVS 保存 |
| 服务器 | YOUR_SERVER_IP | YOUR_SERVER_IP |
| 板卡 fqbn | esp32:esp32:esp32cam | esp32:esp32:esp32s3:PSRAM=opi,... |

---

## 5. 已知待办

1. **DEVICE_KEY 轮换**：`config.h` 里仍是阶段一密钥（新服务器实测仍认它），建议随 H2 安全加固一起轮换。
2. **SERVER_URL 已是新服务器** `YOUR_SERVER_IP`，烧录前确认云端 `/health` 返回 `{"status":"ok"}`。
3. **侧歪阈值**：云端 `SHOULDER_LEVEL_MAX` 建议从 0.03 放宽到 0.10（阶段一已知问题 #3）。
4. **图像朝向待现场确认**：烧录后用 `/capture` 看画面，用 `/rotate` 选定正确编号；若 4 档都不对（如画面旋转 90°），需改代码做像素级旋转。
