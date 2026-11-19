# ESP32-S3-CAM 采图工具包（零安装）

> 目标板：**GOOUUU ESP32-S3-CAM（N16R8：16MB flash + 8MB OPI PSRAM，原生 USB-Serial/JTAG，COM6）**
> 目标机器**不需要装 Python、不需要 arduino-cli**，双击 bat 即可。

---

## 一、烧录（零安装，一次就够）

S3 是**原生 USB**：直接用 USB-C 线连电脑，**不需要 USB-TTL，也不需要 GPIO0 接地**。

1. 双击 **`烧录.bat`**
2. 板型选 `1`（ESP32-S3）
3. 串口直接回车用 `COM6`（脚本会先列出可用串口）
4. 等 1~3 分钟（16MB 镜像）

> 识别不到串口：按住板上的 **BOOT**，点一下 **RST**，再松开 BOOT，然后重跑。

### 烧完验证

按一下 RST，用串口助手或 PowerShell（115200）看输出，期望：

```
[BOOT] ESP32-S3 capture-only (auto pinset)
[SYS] psram=yes flash=16MB
[CAM] freenove/s3-eye OK: 640x480 3xxxx bytes
[WiFi] OK ip=192.168.x.x
[USE]  http://192.168.x.x/capture
```

**固件会开机自动探测三套常见引脚**（Freenove/S3-EYE、XIAO Sense、Waveshare），
GOOUUU 用的是第一套，正常会打印 `freenove/s3-eye OK`。
如果三套都失败会打印 `所有引脚组合都失败`，那就是排线/供电问题。

没有串口助手时用这段拿 IP：

```powershell
$p = New-Object System.IO.Ports.SerialPort 'COM6',115200
$p.Open(); $p.ReadTimeout = 3000
1..40 | ForEach-Object { try { $p.ReadLine() } catch { } }
$p.Close()
```

---

## 二、采图（零安装）

双击 **`采集.bat`**，依次输入：设备 IP → 姿态编号 → 张数（回车=40）。

图片存 `captures\<时间>-<标签>\`，同名 `meta.json` 记录标签与参数。

| 编号 | 标签 | 建议张数 | 怎么摆 |
|---|---|---|---|
| 1 | upright | 40 | 坐直，正常看书/写字 |
| 2 | slouch | 40 | 低头伏案 |
| 3 | lean | 20 | 侧身歪头 |
| 4 | phone | 30 | 低头玩手机 |
| 5 | sleep | 20 | 趴桌 |
| 6 | away | 10 | 人离开（空桌） |
| 7 | unknown | 10 | **手挡镜头 / 关灯 / 转向天花板** —— 别省 |

**机位架好后全程不要动相机**，只改人的姿态。

### 再补两组光照（很重要）

同一机位：开台灯采 20 张 upright，只留屏幕光再采 20 张。
`head_height` 对逆光敏感，这两组能直接决定要不要做亮度自适应——目前完全没考虑。

---

## 三、采完发回来

把 `captures\` 里对应文件夹整个打包发我，我跑标定：

1. 同一套 YOLOv8-pose 跑一遍，统计每类特征分布
2. 找端正/不端正之间的空档，重定 `HEAD_HEIGHT_MIN` / `SHOULDER_LEVEL_MAX`
3. 回放混淆矩阵

---

## 四、包内文件

```
真机采集包/
├── 采集.bat                          ← 双击采图
├── 烧录.bat                          ← 双击烧录（选 1 = S3）
├── capture.ps1                       ← 采集实现
├── flash.ps1                         ← 有 arduino-cli 时的源码级烧录
├── analyze_captures.py               ← 标定分析（服务器上跑）
├── firmware/
│   ├── capture-s3-goouuu-merged.bin  ← 16MB 整包（S3，写 0x0）
│   ├── capture-esp32cam-merged.bin   ← 4MB 整包（旧 AI Thinker，写 0x0）
│   ├── capture_s3/                   ← S3 源码（自动探测引脚）
│   └── capture/                      ← ESP32-CAM 源码
├── tools/esptool.exe                 ← 自带运行时，不需要 Python
└── _mock/mock_cam.py                 ← 假设备，无硬件时自测
```

### S3 编译参数（万一要重编）

```
fqbn: esp32:esp32:esp32s3:PSRAM=opi,FlashSize=16M,PartitionScheme=huge_app,CDCOnBoot=cdc
合并:  merged.bin 写 0x0（S3 bootloader 在 0x0，不是 ESP32 的 0x1000）
```

---

## 五、常见问题

| 现象 | 处理 |
|---|---|
| 烧录报 Could not open COM6 | 按住 BOOT 按 RST 进下载模式；关掉占用的串口助手 |
| 串口全是乱码 | 波特率 115200；USB 线要能传数据 |
| `[CAM] 所有引脚组合都失败` | 摄像头排线没插好/插反，或供电不足（换 USB 口） |
| 采出来全黑 | 先看 `/status` 里 `cam_ok`；黑帧也可能是镜头盖没揭 |
| 连不上设备 | 电脑和设备不在同一 2.4GHz 网络 |
| 单帧 < 3KB | 排线接触不良或太暗，脚本会记为失败并继续 |