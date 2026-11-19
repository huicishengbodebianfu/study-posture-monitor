# 云端说明（FastAPI + OpenVINO）

> 阶段一云端服务：接收 ESP32-CAM 上传的 JPEG，用 YOLOv8-pose 推理关键点，
> 规则引擎判定坐姿是否端正，返回动作指令。

---

## 目录

- [1. 它是干什么的](#1-它是干什么的)
- [2. 目录结构](#2-目录结构)
- [3. 环境准备](#3-环境准备)
- [4. 导出 OpenVINO 模型](#4-导出-openvino-模型)
- [5. 配置](#5-配置)
- [6. 本地测试](#6-本地测试)
- [7. 部署为 systemd 服务](#7-部署为-systemd-服务)
- [8. 阈值标定](#8-阈值标定)
- [9. 接口说明](#9-接口说明)
- [10. 规则引擎](#10-规则引擎)
- [11. 多态检测（多个 AI API 分开工作）](#11-多态检测多个-ai-api-分开工作)
- [12. 常见问题](#12-常见问题)

---

## 1. 它是干什么的

云端服务是整个系统的"大脑"。它做四件事：

1. **收图**：接收 ESP32 上传的 JPEG 照片（校验设备密钥）。
2. **推理**：用 YOLOv8-pose 模型找出照片里人体的关键点（鼻子、肩膀、手肘等）。
3. **判定**：根据关键点位置算出两个特征——"头高"和"肩平差"，
   再和阈值比较，判断坐姿是否端正。
4. **决策**：结合"连续 2 帧"规则，决定是否返回 `blow`（让风扇吹）。

**它不做什么**：不存数据库、不落盘、不保存历史。照片用完即弃，
重启后所有状态清零。这是刻意的隐私设计。

---

## 2. 目录结构

```
cloud/
├── app.py                          # FastAPI 服务主程序（HTTP 接口）
├── posture.py                      # 姿态特征计算 + 规则引擎（核心算法）
├── ai_providers.py                 # 多态检测：Qwen-VL / DeepSeek 两个 AI API 封装
├── multimodal.py                   # 多态检测：快慢双通道编排与融合
├── calibrate.py                    # 阈值标定脚本
├── requirements.txt                # Python 依赖
├── posture.service                 # systemd 单元（开机自启）
├── .env.example                    # 环境变量示例
└── yolov8n-pose_openvino_model/    # OpenVINO 模型目录（需自行导出）
    ├── yolov8n-pose.xml
    └── yolov8n-pose.bin
```

**各 Python 文件的分工**：

| 文件 | 职责 | 什么时候用 |
|------|------|-----------|
| `app.py` | HTTP 服务：收图、鉴权、调算法、返回 JSON | 一直运行 |
| `posture.py` | 算法：关键点 → 特征 → 判定（快速通道） | 被 app.py 调用 |
| `ai_providers.py` | 多态检测：封装 Qwen-VL（看图）与 DeepSeek（决策） | 慢速通道开启时 |
| `multimodal.py` | 多态检测：编排两个 AI + 与快速通道融合 | 慢速通道开启时 |
| `calibrate.py` | 标定：用你的照片算出合适阈值 | 部署后调参时手动跑 |

---

## 3. 环境准备

在 Ubuntu 22.04 服务器上执行：

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip

cd /opt/posture
python3 -m venv venv          # 创建独立 Python 环境
source venv/bin/activate      # 激活环境
pip install -r requirements.txt
```

> **为什么要用 venv？**
> 虚拟环境把项目依赖和系统 Python 隔离开，避免版本冲突。
> 每次登录服务器后，操作前都要先 `source venv/bin/activate`。

---

## 4. 导出 OpenVINO 模型

YOLOv8-pose 原始模型是 PyTorch 格式（`.pt`），需要转成 OpenVINO 格式
（`.xml` + `.bin`）才能在 CPU 上高效推理。

在任意有网络的机器上（或本机）执行：

```bash
pip install ultralytics
python -c "from ultralytics import YOLO; YOLO('yolov8n-pose.pt').export(format='openvino')"
```

执行后会生成 `yolov8n-pose_openvino_model/` 目录，
把它拷贝到 `/opt/posture/` 下即可。

> **为什么用 OpenVINO 而不是直接跑 PyTorch？**
> OpenVINO 是 Intel 的推理加速框架，在 CPU 上比原生 PyTorch 快数倍，
> 且不需要 GPU。对于本项目 0.1fps 的低负载场景，CPU 完全够用。

---

## 5. 配置

复制 `.env.example` 为 `.env` 并修改：

```bash
cp .env.example .env
# 编辑 .env，至少修改 DEVICE_KEY
```

关键环境变量：

| 变量 | 说明 | 默认 |
|------|------|------|
| `DEVICE_KEY` | 设备密钥，需与固件一致 | change-me... |
| `MODEL_DIR` | OpenVINO 模型目录 | yolov8n-pose_openvino_model |
| `HEAD_HEIGHT_MIN` | 头高下限（低于此值判为低头/驼背） | 0.15 |
| `SHOULDER_LEVEL_MAX` | 肩平差上限（高于此值判为侧歪） | 0.05 |
| `CONF_THRESHOLD` | 人体检测置信度阈值 | 0.5 |
| `MULTIMODAL_ENABLED` | 是否启用多态检测慢速通道 | false |
| `QWEN_API_KEY` | 通义千问 API Key（AI-1 看图） | 空 |
| `QWEN_MODEL` | Qwen 视觉模型名 | qwen-vl-plus |
| `DEEPSEEK_API_KEY` | DeepSeek API Key（AI-2 决策） | 空 |
| `DEEPSEEK_MODEL` | DeepSeek 模型名 | deepseek-chat |
| `MULTIMODAL_INTERVAL` | 慢速通道抽查周期（秒） | 30 |

**两个阈值怎么理解**：

- `HEAD_HEIGHT_MIN`：头高 = 肩中点Y − 鼻Y（归一化）。
  坐直时头高较大，低头/驼背时变小。**低于**这个值就判为不端正。
- `SHOULDER_LEVEL_MAX`：肩平差 = 左右肩Y差的绝对值。
  坐正时两肩齐平（差值小），侧歪时变大。**高于**这个值就判为不端正。

> 这两个默认值是"经验值"，实际用之前**强烈建议**按
> [第 8 节](#8-阈值标定) 用你自己的照片重新标定。

---

## 6. 本地测试

```bash
source venv/bin/activate
uvicorn app:app --host 0.0.0.0 --port 8000
```

**健康检查**（确认服务活着）：

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

**上传一张图片测试**：

```bash
curl -X POST http://localhost:8000/detect \
  -H "X-Device-Key: change-me-to-a-random-secret" \
  -H "Content-Type: image/jpeg" \
  --data-binary @test.jpg
# {"action":"none","upright":true,"head_height":0.21,"shoulder_level":0.01,"reason":""}
```

---

## 7. 部署为 systemd 服务

systemd 可以让服务开机自启、崩溃自动重启。

```bash
sudo cp posture.service /etc/systemd/system/posture.service
# 按需修改 User / WorkingDirectory / Environment
sudo systemctl daemon-reload
sudo systemctl enable --now posture
sudo systemctl status posture
```

**查看实时日志**：

```bash
journalctl -u posture -f
```

**常用命令**：

| 命令 | 作用 |
|------|------|
| `sudo systemctl start posture` | 启动 |
| `sudo systemctl stop posture` | 停止 |
| `sudo systemctl restart posture` | 重启（改配置后） |
| `sudo systemctl status posture` | 查看状态 |

---

## 8. 阈值标定

默认阈值不一定适合你的摄像头位置和坐姿习惯，建议标定一次。

1. 用 CameraWebServer 的 MJPEG 流在浏览器抓帧，分两组：
   - `samples/upright/`：端正 30 张
   - `samples/slouch/`：低头/侧歪 30 张
2. 运行标定脚本：

```bash
python calibrate.py --upright ./samples/upright --slouch ./samples/slouch
```

3. 脚本会输出两组样本的 `head_height` / `shoulder_level` 分布
   （均值、P5、P50、P95），并给出**建议阈值**。
4. 把建议阈值写入 `.env` 或 `posture.service`，重启服务：

```bash
sudo systemctl restart posture
```

> 详细标定方法见 [../docs/标定流程.md](../docs/标定流程.md)

---

## 9. 接口说明

### GET /health

健康检查，返回 `{"status":"ok"}`。用于确认服务是否正常运行。

### POST /detect

**请求头**：

| 头 | 值 | 说明 |
|----|-----|------|
| `X-Device-Key` | 设备密钥 | 必须与 `.env` 中一致，否则 401 |
| `Content-Type` | `image/jpeg` | 固定值 |

**请求体**：JPEG 二进制数据。

**响应 JSON**：

| 字段 | 说明 |
|------|------|
| `action` | `blow`（触发风扇）/ `none`（不触发） |
| `upright` | `true` 端正 / `false` 不端正 / `null` 无法判定 |
| `head_height` | 归一化头高（越大越挺直） |
| `shoulder_level` | 归一化肩平差（越小越平） |
| `reason` | 无效原因（如 `no_person`），正常时为空 |

**响应示例**：

```json
// 坐姿端正
{"action":"none","upright":true,"head_height":0.21,"shoulder_level":0.01,"reason":""}

// 连续 2 帧不端正，触发吹风
{"action":"blow","upright":false,"head_height":0.08,"shoulder_level":0.02,"reason":""}

// 画面里没人 / 太暗
{"action":"none","upright":null,"head_height":null,"shoulder_level":null,"reason":"no_person"}
```

---

## 10. 规则引擎

规则引擎决定"什么时候该吹"，逻辑如下：

- **连续 2 帧不端正 → 触发 `blow`**
  （单帧不吹，避免瞬时动作误报）
- **触发后 60 秒内去重**，不重复触发
  （避免一直吹个不停）
- **恢复端正立即返回 `none`**
  （坐直了马上停）
- **关键点置信度不足 / 无人 → 视为"无法判定"，不触发**
  （宁漏报不误报）

**实现方式**：无数据库、无状态持久化，用一个内存里的滑动窗口
记录最近几帧的判定结果。服务重启后窗口清空，重新开始计数。

---

## 11. 多态检测（多个 AI API 分开工作）

> **默认关闭**，不影响阶段一验收。这是一个可选的"增强层"，
> 让**多个 AI API 各司其职、分开工作**，与本地 YOLOv8-pose 形成互补。

### 11.1 为什么要多态检测

本地 YOLOv8-pose 是"快速通道"：快（60~150ms）、免费、离线，
但只能看**骨骼姿态**，看不懂"在干什么"。

多态检测是"慢速通道"：慢（秒级）、按量计费、需联网，
但能理解**语义行为**（如玩手机、发呆、低头看屏幕）。

两者**融合**：任一判定不端正即触发提醒，兼顾速度与准确度。

### 11.2 两个 AI 如何分工

| 角色 | 模型 | 职责 | 输入 → 输出 |
|------|------|------|-------------|
| **AI-1 视觉分析** | Qwen-VL（通义千问） | 图像获取 / 看图 | JPEG → 结构化视觉描述（姿态、是否看手机、是否在画面） |
| **AI-2 行为决策** | DeepSeek | 行为分析 / 决策 | 视觉描述 → 行为判定 + `action` |

**为什么拆成两个 API？**
- 各司其职：视觉模型擅长"看"，文本模型擅长"推理决策"。
- 可独立替换：换视觉模型不影响决策逻辑，反之亦然。
- 成本可控：慢速通道按 `MULTIMODAL_INTERVAL` 抽查，不必每帧都调。

### 11.3 数据流

```
ESP32 JPEG
   │
   ├──▶ 快速通道：YOLOv8-pose → 特征 → 判定 ─┐
   │                                          ├──▶ 融合 → 规则引擎 → action
   └──▶ 慢速通道：Qwen-VL 看图 → DeepSeek 决策 ┘
              （每 MULTIMODAL_INTERVAL 秒抽查一次，异步不阻塞）
```

### 11.4 如何开启

1. 在 `.env` 中填入两个 API Key：

```bash
MULTIMODAL_ENABLED=true
QWEN_API_KEY=sk-xxxxxxxx        # 阿里云 DashScope
DEEPSEEK_API_KEY=sk-xxxxxxxx    # DeepSeek 开放平台
MULTIMODAL_INTERVAL=30          # 每 30 秒抽查一次
```

2. 重启服务：

```bash
sudo systemctl restart posture
```

3. 观察日志，确认慢速通道生效：

```bash
journalctl -u posture -f | grep multimodal
```

### 11.5 融合策略

| 快速通道 | 慢速通道 | 融合结果 |
|----------|----------|----------|
| 端正 | 端正 | 端正（`none`） |
| 端正 | 不端正 | **不端正**（慢速通道可"翻案"） |
| 不端正 | 端正 | **不端正**（快速通道优先） |
| 无法判定 | 不端正 | 不端正 |
| 无法判定 | 无结果 | 无法判定（不触发） |

> 原则：**任一通道判定不端正即视为不端正**，宁可多提醒也不漏报。
> 慢速通道结果有缓存有效期，过期后回退到快速通道。

### 11.6 成本与限流

- 慢速通道按 `MULTIMODAL_INTERVAL` 抽查，默认 30 秒一次。
- 一次学习时段（4 小时）约调用 480 次，费用取决于所选模型单价。
- 建议先用 `qwen-vl-plus` + `deepseek-chat` 这类性价比模型验证，
  确认有效后再考虑升级。

---

## 12. 常见问题

| 现象 | 排查 |
|------|------|
| 启动报"模型目录不存在" | 确认已导出 OpenVINO 模型并放到 MODEL_DIR |
| 401 invalid device key | DEVICE_KEY 与固件不一致 |
| 一直返回 no_person | 光照不足 / 人不在画面 / CONF_THRESHOLD 过高 |
| 误报频繁 | 重新标定阈值，或调高 HEAD_HEIGHT_MIN |
| 漏报 | 调低 HEAD_HEIGHT_MIN / 调高 SHOULDER_LEVEL_MAX |
| 服务起不来 | 看 `journalctl -u posture -f` 的报错信息 |
| 端口被占用 | 换端口，或 `sudo lsof -i:8000` 查占用进程 |
