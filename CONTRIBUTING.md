# 贡献指南 (Contributing Guidelines)

欢迎为「学习状态监测系统」贡献代码、文档或硬件设计。本项目面向学生与 IoT 爱好者，
鼓励小步提交、明确描述、重视隐私与安全。

## 目录

- [我可以贡献什么](#我可以贡献什么)
- [提交前必读：隐私与红线](#提交前必读隐私与红线)
- [分支与提交规范](#分支与提交规范)
- [代码风格](#代码风格)
- [Pull Request 流程](#pull-request-流程)
- [本地验证](#本地验证)
- [问题与讨论](#问题与讨论)

---

## 我可以贡献什么

- **固件**：ESP32-CAM / ESP32-S3-CAM 的 Arduino 代码（`firmware/`、`阶段二/firmware/`）
- **云端**：FastAPI 服务、姿态判定、多模态慢通道（`cloud/`、`阶段二/server-sync/`）
- **文档**：部署指南、接线说明、标定流程、故障排查（`docs/`、`阶段二/慢通道选型/*.md`）
- **硬件**：接线图、BOM、PCB 布局（`阶段二/硬件资料/`）
- **测试**：单元测试、模拟 ESP32 上传、标定数据集（`阶段二/慢通道选型/tools/`）
- **CI / 工具链**：GitHub Actions、lint、构建检查

---

## 提交前必读：隐私与红线

> 本项目由真实学生项目整理而来，**已做数据脱敏**。维护这条红线比任何功能都重要。

### 严禁提交的内容

| 类别 | 示例 | 处理方式 |
|---|---|---|
| **密钥与凭据** | `DEVICE_KEY`、`API_KEY`、`sk-…`、WiFi 密码、SSH 私钥 | 用 `CHANGE_ME_*` 占位；真实值只写在本地 `.env`（已被 `.gitignore` 忽略） |
| **真实 IP / 主机名** | 公网 IP、内网 IP、实例 ID、主机名、地域 | 用 `YOUR_SERVER_IP` / `YOUR_PRIVATE_IP` / `<your-instance-id>` |
| **人脸抓图 / 实拍照片** | `.jpg`、`images/raw/`、`data/captures*/` | 本仓库**不收录**任何实拍图像；测试用图请走占位或合成 |
| **编译产物 / 大体积数据** | `.bin`、`.exe`、`.pyc`、`.zip`、`.jsonl`、`.csv`、模型权重 | 已被 `.gitignore` 排除；模型需使用者自行准备 |
| **第三方 vendored 代码** | `esp32-camera-master` 等乐鑫库 | 通过 Arduino 库管理器获取，不随仓库分发 |

### 提交前自检

在 `git push` 之前，跑一遍本地检查脚本（见 [本地验证](#本地验证)）：

```bash
python .github/scripts/check_secrets.py
```

如果报告任何命中，**不要提交**，先清除对应内容。

### 如果你不小心提交了敏感信息

1. 立即撤销/轮换对应密钥（API Key、DEVICE_KEY、WiFi 密码、SSH 密钥）。
2. 用 `git reset` 或 `git filter-repo` 从**历史提交**中彻底移除（仅 `git rm` 不够）。
3. 通知维护者（提 issue 或邮件），以便我们评估是否需要归档仓库重建。

---

## 分支与提交规范

### 分支

- `main`：稳定分支，直接推送需通过 CI 且经审阅。
- 功能/修复分支：从 `main` 派生，命名建议
  - `feat/xxx`（新功能）
  - `fix/xxx`（Bug 修复）
  - `docs/xxx`（纯文档）
  - `hw/xxx`（硬件设计）
  - `ci/xxx`（CI / 工具链）

### 提交信息

建议遵循 [Conventional Commits](https://www.conventionalcommits.org/)，中文亦可：

```
feat(firmware): ESP32-S3 增加电池电量 ADC 监测

- 新增分压电阻读取电池电压
- 低电量时黄灯闪烁并上报 battery_low
- 标定阈值写死 3.3V，后续可改为云端下发

fix(cloud): 修复连续帧计数在多 worker 下失效
docs: 补充标定流程的拍照距离说明
```

每条提交只做一件事；不要把"改配置 + 修 bug + 加文档"混在一个提交里。

---

## 代码风格

- **Python**：4 空格、行宽 ≤120、类型注解（新代码）、`flake8` 无报错。
  关键模块（`posture.py`、`multimodal.py`、`schema.py`）请附简短单元测试。
- **C/C++（Arduino）**：与项目现有风格一致，函数与常量加注释说明引脚/时序。
- **Markdown**：UTF-8 无 BOM，表格对齐，新增文档请在 `docs/README.md` 或对应索引中注册。
- **硬件图**：优先提供矢量源文件（`.svg`）+ 渲染图（`.png`），方便后续修改。

---

## Pull Request 流程

1. 在 `main` 上跑通你的改动（本地验证见下）。
2. 推送到你的 fork 或分支，创建 PR。
3. PR 描述请包含：
   - 解决了什么问题 / 新增什么能力
   - 如何测试（命令、照片、预期输出）
   - 是否影响现有配置（若是，说明迁移步骤）
   - 截图 / 日志（**请勿包含 IP、密钥、人脸**）
4. CI 自动运行：机密扫描 + Python 语法 + flake8 lint。
5. 至少 1 人审阅通过后合并到 `main`。

> 小修改（错别字、注释、README 链接）可直接合并，不必等审阅。

---

## 本地验证

仓库提供检查脚本，在推送到 GitHub 之前必跑：

```bash
# 1. 机密 / 隐私残留扫描（退出码非 0 表示发现疑似敏感内容）
python .github/scripts/check_secrets.py

# 2. Python 语法检查
python -m compileall -q \
  学习状态监测系统/cloud 学习状态监测系统/tools \
  阶段二/server-sync 阶段二/慢通道选型/deploy 阶段二/慢通道选型/proposal \
  阶段二/慢通道选型/tools 阶段二/真机采集包

# 3. 风格检查（需先 pip install flake8）
#    核心模块（cloud / tools / server-sync / deploy）必须通过；
#    proposal/ 与 tools/ 是内部评估/抓取脚本，已在 .flake8 中 exclude。
#    Windows PowerShell 用户请把 find 换成 Get-ChildItem -Recurse -Filter *.py
find 学习状态监测系统/cloud 学习状态监测系统/tools 阶段二/server-sync \
     阶段二/慢通道选型/deploy -name '*.py' \
  | xargs flake8 --max-line-length=120 --extend-ignore=E203,W503
```

> 提示：仓库根目录的 `.flake8` 定义了 `exclude` 与 `per-file-ignores`，
> 本地跑 `flake8`（不带路径）时会沿用同一套规则，与 CI 一致。

脚本会忽略已知的安全占位符（`CHANGE_ME_*`、`YOUR_*`、`sk-xxxxxxxx` 等），
只拦截高置信度的真实凭据。

---

## 问题与讨论

- Bug / 安全漏洞 → 提 [Issue](../../issues)，安全漏洞请走私信或邮件，**不要在公开 issue 里贴密钥或 IP**。
- 功能建议 / 硬件选型讨论 → Issue 或 Discussion。
- 想新增检测能力（如玩手机、发呆）→ 先开 Issue 讨论接口契约，再动手写代码。

---

## 许可

本项目采用 [MIT 许可](../LICENSE)。贡献即表示你同意以 MIT 协议发布你的贡献。
