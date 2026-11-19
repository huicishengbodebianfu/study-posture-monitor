# 学习状态监测系统（开源版）

一个「坐姿端正检测 + 风扇提醒」的物联网学习助手小项目。

ESP32-CAM / ESP32-S3-CAM 定时拍照上传，云端用 YOLOv8-pose（OpenVINO）做快速通道姿态判定，
可选接入多模态大模型做「慢通道」语义判断；判定坐姿不端正时，让小风扇吹一下并亮灯提醒。

> 本项目由学生科技创新项目整理而来，**已做数据脱敏**：所有密钥、服务器地址、
> WiFi 凭据、SSH 私钥、含人脸的抓图、第三方 vendored 代码与编译产物均未包含在此仓库中。
> 详见 [数据脱敏与排除说明](数据脱敏与排除说明.md)。

## 仓库结构

```
study-posture-monitor/
├── README.md                 ← 本文件（导航）
├── LICENSE                   ← MIT
├── .gitignore
├── 数据脱敏与排除说明.md       ← 开源前脱敏/排除清单
├── 学习状态监测系统/           ← 阶段一权威源码（固件 + 云端 + 文档）
│   ├── firmware/              ESP32-CAM 固件（main.ino / config.h）
│   ├── cloud/                 FastAPI + YOLOv8-pose 服务、.env.example
│   ├── tools/                 抓图/转发辅助脚本
│   └── docs/                  项目文档、说明文档、使用说明、接线图、部署指南等
└── 阶段二/                    ← 阶段二增量成果
    ├── firmware/              ESP32-S3-CAM 固件源码
    ├── server-sync/           云端慢通道代码（app / ai_providers / multimodal / schema / 测试）
    ├── 慢通道选型/             模型选型与设计报告、部署/提案/工具脚本（*.py, *.md）
    ├── 硬件资料/               原创接线图（.md/.svg/.png）、BOM、引脚定义
    ├── 真机采集包/             采集/烧录脚本与固件源码
    └── 项目文档.md             阶段二设计与状态总览
```

## 快速开始

1. **读文档**：先看 [学习状态监测系统/README.md](学习状态监测系统/README.md)，再按
   [部署指南](学习状态监测系统/docs/部署指南.md) 与 [使用说明](学习状态监测系统/docs/使用说明.md) 动手。
2. **改配置**：把 `学习状态监测系统/firmware/config.h` 与 `cloud/.env.example`
   （复制为 `.env`）中的 `YOUR_*` / `CHANGE_ME_*` 占位符改成你自己的值。
   `DEVICE_KEY` 必须与固件、云端三方一致。
3. **部署**：云端需自备 OpenVINO 版 yolov8n-pose 模型（本仓库不含模型文件），
   参考 `学习状态监测系统/cloud/README.md`。
4. **烧录**：用 Arduino IDE / arduino-cli 编译 `firmware/` 下的 `.ino` + `config.h`。

## 配置占位符一览

| 占位符 | 含义 | 你需要填 |
|---|---|---|
| `YOUR_SERVER_IP` | 云端服务器公网地址 | 你的云主机 IP 或域名 |
| `YOUR_WIFI_SSID` / `YOUR_PHONE_HOTSPOT` | WiFi 名称 | 你的 2.4GHz WiFi |
| `YOUR_WIFI_PASSWORD` / `YOUR_WIFI_PASSWORD_2` | WiFi 密码 | 你的密码 |
| `CHANGE_ME_DEVICE_KEY` | 设备鉴权密钥 | 随机字符串，固件与 `.env` 保持一致 |
| `CHANGE_ME_API_KEY` / 各 `*_API_KEY` | 大模型 API Key | 到对应平台自行申请 |

> 慢通道涉及的第三方模型网关（DeepSeek / DashScope / SenseNova / ChatAnywhere / ModelScope 等）
> 均为**可选**能力，需自行申请账号与 Key；项目默认不启用，不影响快速通道运行。

## 隐私提示

本项目涉及摄像头采集，**请仅在你知情同意的范围内使用**，不要把含人脸的真实照片
上传到公开仓库或第三方服务。本仓库已排除所有实拍抓图，仅保留文字记录与示意图。

## 不包含的内容

编译后的固件镜像（`.bin`）、`esptool.exe`、OpenVINO 模型文件、真机抓图/照片、
SSH 私钥、`.env` 真实配置文件、第三方 vendored 库（如 `esp32-camera-master`）、
大体积评估数据（`.csv/.jsonl`）、厂商数据手册 PDF 等，均**不在**本仓库中。
构建与烧录所需的外部工具、模型请自行获取。

## 许可

MIT，见 [LICENSE](LICENSE)。
