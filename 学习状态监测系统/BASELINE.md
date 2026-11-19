# 基线说明（权威版本）

> 本目录是**后续开发的唯一权威版本**。
> 建立日期：2026-10-02

## 一、这个版本从哪来

本目录内容来自同伴发来的交接包：

```
交接包-20261002/学习状态监测系统（新增多态API检测）/
```

即名为「**新增多态API检测**」的版本（下称 **B 版**）。它与**线上正在运行的服务逐字对应**，已实测验证：

| 证据 | 说明 |
|------|------|
| 线上 `/detect` 返回 `state` / `source` 字段 | 只有 B 版 `app.py` + `multimodal.fuse()` 会产生这两个字段 |
| 交接包 `备份/cloud_env/.env` 中 `DEVICE_KEY` | 与线上接口实际接受的密钥完全一致 |
| 交接包 `备份/` 标注的服务器 IP | 即线上接口地址 `YOUR_SERVER_IP` |
| 备份 5 个关键文件 MD5 | 已逐项校验，**5/5 全部 OK** |

## 二、已做的本地化改动

本地化改动如下（**软件文件**除 `firmware/config.h` 与 `cloud/.env.example` 外仍与原交接包逐字节相同；**文档类**已按 2026-10 硬件选型决策更新，见下表，不再与原交接包逐字一致）：

| 文件 | 改动 |
|------|------|
| `firmware/config.h` | `SERVER_URL` → `http://YOUR_SERVER_IP:8000/detect`；`DEVICE_KEY` → 线上真实密钥 |
| `cloud/.env.example` | `DEVICE_KEY` 填真实密钥；阈值改为**实测标定值** `HEAD_HEIGHT_MIN=0.37` / `SHOULDER_LEVEL_MAX=0.03`（原模板是会导致误报的 0.15/0.05），并加警告注释 |
| `README.md`、`docs/接线图.md`、`docs/部署指南.md`、`firmware/README.md` | 硬件选型按 2026-10 决策更新：电芯**带保护板**、升压改 **XL3608-5V**、风扇改 **5V 无刷涡轮鼓风机**、GPIO 统一 **12/13**、修续航口径（约 8~11h） |

## 三、B 版相对 A 版（旧版）的关键差异

旧的 `项目/学习状态监测系统/`（下称 **A 版**）已保留作历史，不作为开发基线。B 版比 A 版多出/修好了这些：

| 项 | A 版 | **B 版（本目录）** |
|---|---|---|
| `cloud/ai_providers.py` | ❌ 无 | ✅ Qwen-VL + DeepSeek 两个 AI API 封装 |
| `cloud/multimodal.py` | ❌ 无 | ✅ 快/慢双通道编排与融合 |
| `cloud/app.py` | 仅快速通道，无 `state`/`source` | ✅ 双通道融合，返回 `state`/`source` |
| `cloud/posture.py` | ❌ 含 **float32 JSON 序列化 500 崩溃** bug | ✅ 已显式 `float()` 修复 |
| `cloud/posture.service` | ❌ 无 `EnvironmentFile`（`.env` 不生效）、要求不存在的用户 | ✅ 已修（`EnvironmentFile` + `User=root`） |
| `tools/capture_server.py` | ❌ 无 | ✅ 手机拍照标定工具 |
| `docs/标定流程.md` | ❌ 无 | ✅ 有 |
| 阈值 | 默认 0.15/0.05（必误报） | ✅ 已标定 0.37/0.03 |

> 详见交接包 `交接文档.md` 第 2.5、2.7 节。

## 四、当前已确认的线上状态

| 项 | 值 |
|---|---|
| 接口 | `http://YOUR_SERVER_IP:8000/detect`（POST） |
| 健康检查 | `http://YOUR_SERVER_IP:8000/health` → `{"status":"ok"}` ✅ 实测 |
| 设备密钥 | 见 `firmware/config.h` / `cloud/.env.example` |
| 生效阈值 | `0.37 / 0.03`（线上 `.env` 覆盖） |
| 多态检测（慢速通道） | **当前关闭**（`MULTIMODAL_ENABLED=false`）→ `source:"fused"` 只是两通道都判不出时的兜底标记 |
| 实测验证 | 已用两张真实图片 POST 成功，分别命中 `fast` / `fused` 路径 |

## 五、❗ 关键待办

| # | 事项 | 严重度 |
|---|------|--------|
| 1 | **服务器 2026-10-25 到期**。不续费则 IP 必变、要重烧固件、重导模型。重建包在 `../交接包-20261002/备份/`（含模型 + `.env` + `rebuild.sh`，重建 3~6 分钟） | 🔴 高 |
| 2 | **侧歪检测不可靠**：标定样本区分度小（坐直 0.0050 vs 驼背 0.0065），需重采更明显的侧歪样本标定 | 🟡 中 |
| 3 | **`DEVICE_KEY` 已泄露**（曾出现在对话记录中）。如在意：`openssl rand -hex 16` 重新生成，同步改 `/opt/posture/.env` 和 `firmware/config.h` 后重启 | 🟡 中 |
| 4 | **硬件侧未完成**：ESP32-CAM 烧录、风扇/LED/按钮接线、全链路联调 | ⬜ 待做 |

## 六、开发前必读

1. **不要改** `firmware/config.h` 里的行为参数（`UPLOAD_INTERVAL_MS=10000`、`HTTP_TIMEOUT_MS=8000`、`CAM_FRAME_SIZE=FRAMESIZE_VGA`），已实测调好。
2. 上传间隔是 **10 秒**（不是文档说的 20 秒），因此「连续 2 帧」约 **20 秒**触发。
3. systemd 的 `--workers` **必须是 1**，多进程会让「连续 2 帧」计数器失效。
4. 文档里 `0.21 / 0.09 / 0.15 / 0.045` 等示例数值与实测**差 3~5 倍，是错的**，一律以标定值 `0.37 / 0.03` 为准。

## 七、目录结构

```
学习状态监测系统/
├── BASELINE.md                 本文件
├── README.md                   项目总览（交接包原版）
├── cloud/                      云端服务（部署到服务器）
│   ├── app.py                  FastAPI 服务：收图、鉴权、推理、返回 JSON
│   ├── posture.py              核心算法：关键点 → 特征 → 判定 + 规则引擎
│   ├── ai_providers.py         Qwen-VL / DeepSeek 两个 AI API 封装（可选层）
│   ├── multimodal.py           快慢双通道编排与融合
│   ├── calibrate.py            阈值标定脚本
│   ├── requirements.txt        依赖（已钉死实测版本）
│   ├── posture.service         systemd 单元（已修正）
│   └── .env.example            环境变量模板（已填真实值 + 标定阈值）
├── firmware/                   ESP32-CAM 固件
│   ├── main.ino                主程序：非阻塞状态机
│   ├── config.h                可调参数（已填服务器地址与密钥）
│   └── README.md               烧录说明
├── tools/
│   ├── capture_server.py       手机拍照测试工具（标定用）
│   └── README.md
└── docs/
    ├── 接线图.md
    ├── 部署指南.md
    └── 标定流程.md
```

> 服务器快照（模型 / `.env` / 重建脚本）在 `../交接包-20261002/备份/`。
