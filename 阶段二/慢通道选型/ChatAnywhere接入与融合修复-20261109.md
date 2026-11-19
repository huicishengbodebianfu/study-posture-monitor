# ChatAnywhere 接入 + 融合缺陷修复（2026-11-09）

## 一、新网关实测

`https://api.chatanywhere.tech/v1`，31 个模型。逐个体检（带图探活 + 3 张真值图）：

| 模型 | 探活 | 命中 | 均延迟 | 结论 |
|---|---|---|---|---|
| **gpt-4.1-mini** | 5.17s | **3/3** | **4.06s** | ✅ 已接入 |
| gpt-5-mini | 2.49s | 3/3 | 5.58s | 可用（次选） |
| gpt-4o | 3.41s | 2/3 | 4.15s | ❌ 漏判低头 |
| gpt-5-nano | 6.26s | 2/3 | 10.96s | ❌ 慢且漏 |
| deepseek-v4-flash | 5.24s | 1/3 | 3.89s | ❌ |
| gpt-5.4 / deepseek-v4.1-flash / gpt-4o-mini / gpt-5 / gpt-4.1 / gpt-5.4-mini | 200 | 未测 | — | 备用候选 |

**已接入 `fallback11 = gpt-4.1-mini`**，启动探活 **2.00s**（池内最快）。
后端总数 12 个 / 6 个账号。

---

## 二、顺带发现并修复的 3 个真缺陷

### 缺陷 1：慢通道看的是倒置原图

快速通道会翻转 180°，但传给慢通道的仍是 `body` 原字节 → **两条通道看的是不同方向的同一帧**。
实测同一帧：不旋转判 `unknown`，旋转后判 `lean`，结论完全不同。

**修复**：翻转生效时，重新编码旋转后的 JPEG 再交给慢通道。

### 缺陷 2：慢通道的姿态类误判会把端正帧吹掉（真误报）

实测：`173834/frame-0010` 是端正组，快通道 `head_height=0.5085` 判端正，
慢通道却说"头部明显低于肩膀"（conf 0.90）并触发 `blow`。

根因：融合里"慢通道不端正"排在"快通道端正"之前。可这个顺序又是**手机检测的生命线**
（学生坐着玩手机时，几何是端正的）。

**修复**：按"几何管姿态、语义管语义"拆分：

| 慢通道状态 | 与快通道"端正"冲突时 |
|---|---|
| slouch / lean / sleep（姿态类） | **采信几何**（几何就是量这个的） |
| phone / daze / away（语义类） | **采信慢通道**（几何看不见） |

单测 5/5 通过；该误报帧复测已恢复 `upright=true, display=UP`。

### 缺陷 3：陈旧慢通道结论会把"看不清"变成"端正"

120 帧回归测出：**趴下组 29 帧被判成 upright**。

根因链：
1. 关键点无效时，帧指纹退化成 `(0.0, 0.0)` → 同场景判断形同虚设
2. 融合第 6 条（慢通道说端正 → 返回端正）**没有同场景闸门**
3. 于是 40 秒前"端正组"留下的缓存把趴下帧全"救"成了端正

**修复**：
- 看不清头部时，用 **16×16 感知哈希**当帧指纹（256 位，汉明距离 ≤32 视为同场景）
- 慢通道单独决定的**全部三条路径**（不端正/无法判定/端正）都加同场景闸门
- 缺指纹一律不采信

---

## 三、120 帧回归结果（修复前 vs 修复后）

| 组 | 修复前 | 修复后 |
|---|---|---|
| 端正 A | 40/40 True | **40/40 True**，0 误吹 |
| 端正 B | 40/40 True（含 1 DIM） | 30 True + **10 判"看不清"**，0 误吹 |
| **趴下** | **29 True**（危险！） | **0 True**，11 SL，29 "看不清" |

趴下组的 29 帧现在归为"看不清"而不是"端正"——保守但正确：
这些帧 YOLO 连人都没检出，几何与慢通道都无法确认，**不能假装看得见**。

---

## 四、当前池子状态

| 项 | 值 |
|---|---|
| 后端 | 12 个 / 6 个账号 |
| 并发 | 3 路，quorum 2，deadline 15s |
| 近 20 分钟 | 完成 6 次，失败 0，熔断 0 |
| 参与分布 | sensenova 4 / Intern-S2 4 / **gpt-4.1-mini 2** / Sage 1 |
| 内存 | 133 MB |

---

## 五、变更与回滚

| 文件 | 变更 |
|---|---|
| `ai_providers.py` | `analyze(image_bytes, hint)`，hint 追加到提示词 |
| `multimodal.py` | `hint` 全链路透传；融合按姿态/语义拆分；`_same_scene` 支持两种指纹；慢通道单边决策加同场景闸门 |
| `app.py` | 翻转后同步给慢通道；帧指纹区分几何/哈希；`stats` 增加感知哈希 |
| `.env` | `AI_FALLBACK11_*`（ChatAnywhere gpt-4.1-mini） |

回滚点：
- `/opt/posture/_backup_fb11/20261109-151308/`
- `/opt/posture/_backup_scene/`

---

## 六、追加两个 ChatAnywhere 账号（2026-11-09 15:25）

两个新 key 未写网关，逐个试了 4 个已知网关：

| key | chatanywhere | sensenova | discovery | modelscope |
|---|---|---|---|---|
| `sk-bG3xtkH0…Pntgd0` | **200 / 31 模型** | 401 | 401 | 200（不校验鉴权，不算数） |
| `sk-wR8a3zqz…J2Io6v` | **200 / 31 模型** | 401 | 401 | 200（同上） |

确认都是 ChatAnywhere 账号。带图实测：

| key | gpt-4.1-mini | gpt-5-mini |
|---|---|---|
| A | 200 / 5.66s / 有 | 200 / 5.72s / 有 |
| B | **200 / 2.77s** / 有 | 200 / 2.89s / 有 |

已接入：
- `fallback12 = gpt-4.1-mini`（key A）
- `fallback13 = gpt-4.1-mini`（key B）

`app.py` 槽位上限从 `range(1,13)` 扩到 `range(1,21)`，以后加后端不用改代码。

### 现况

| 项 | 值 |
|---|---|
| 后端 | **14 个 / 8 个账号** |
| 账号分组 | sensenova×3、discovery×6、modelscope×1、**chatanywhere×3** |
| 启动探活 | fallback11 1.42s / fallback12 1.99s / fallback13 1.86s |
| 实测投票 | `vote2/2 model=sensenova-6.8-flash-lite,gpt-4.1-mini` |

> 启动时 discovery 的 kimi-k2.6 与 qwen3.8-27b 返回 429（限流），由熔断器隔离，
> 不影响其它后端；冷却 120s 后自动恢复。

回滚点：`/opt/posture/_backup_fb1213/20261109-152349/`

---

## 七、OpenRouter 接入（免费模型，2026-11-09 15:45）

### 7.1 密钥与额度

| 密钥 | 用途 | 结果 |
|---|---|---|
| `sk-or-v1-6c0b…bec2e9` | 管理密钥 | 可查配额，**不能推理** |
| `sk-or-v1-2502…905c7` | 推理密钥 | 正常，`limit=null`，`usage=0` |

管理接口 `/credits` 返回 **`total_credits: 0`** —— 账户没有充值，
所以**只能用 `:free` 模型**，且免费档限制为 20 次/分、**50 次/天**。

> 50 次/天对生产来说太少了（我们每 30 秒一次抽查 = 120 次/小时），
> 这个网关只能当**最后兜底**，不能作为常规后端。

### 7.2 免费视觉模型实测（9 个）

| 模型 | 表现 | 结论 |
|---|---|---|
| **nvidia/nemotron-3.5-super-vl:free** | 12.3s，state=upright conf=0.9 | ✅ **已接入** |
| nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free | 同样推理型，需≥2000 token | 备选 |
| google/gemma-4-31b-it:free | HTTP 429（Google provider 限流） | ❌ |
| google/gemma-4-26b-a4b-it:free | HTTP 429 | ❌ |
| dots-studio/dots-3-note-preview:free | 27s，content 恒空 | ❌ |
| openrouter/free（自动路由） | 12s，content 恒空 | ❌ |
| stealth/crouton-alpha | HTTP 403 | ❌ |
| google/lyria-3-clip-preview / lyria-3-pro-preview | 音乐生成模型，不适用 | ❌ |

### 7.3 关键坑：推理模型的 token 分配

`nemotron-3.5-super-vl` 是推理型，输出分两段：

```
message.reasoning = "用户问的是…1.分析图像…3.得出结论：是"   ← 877 tokens
message.content   = '{"state":"upright",...}'              ← 269 字符
```

`max_tokens=800` 时推理把预算吃光，**content 直接是空字符串**——看起来像"模型不支持"，
实际是截断。必须 ≥2000。

试过的两种省 token 手段都没用：
- `reasoning: {"effort":"low"}` → 仍然 800 全用完，content 空
- `reasoning: {"exclude":true}` → 推理照跑，content 被截成 82 字符的坏 JSON

### 7.4 代码改动：按后端覆盖 max_tokens

`Backend` 原本就有 `max_tokens` 字段，但 `app.py` 构造时统一用全局值。改为：

```
AI_FALLBACK{n}_MAXTOKENS   # 不填则用全局 AI_MAX_TOKENS
```

### 7.5 接入配置

```
AI_FALLBACK14_BASE     = https://openrouter.ai/api/v1
AI_FALLBACK14_KEY      = sk-or-v1-2502…905c7
AI_FALLBACK14_MODEL    = nvidia/nemotron-3.5-super-vl:free
AI_FALLBACK14_TIMEOUT  = 16
AI_FALLBACK14_MAXTOKENS= 2000
```

启动探活 1.74s。经单后端池验证：12.33s，`state=upright conf=0.9`。

### 7.6 现况

| 项 | 值 |
|---|---|
| 后端总数 | **15 个 / 9 个账号** |
| 账号分组 | sensenova×3、discovery×6、modelscope×1、chatanywhere×3、**openrouter×1** |

回滚点：`/opt/posture/_backup_or/20261109-154031/`
