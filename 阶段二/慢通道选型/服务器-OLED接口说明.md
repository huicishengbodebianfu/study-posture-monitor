# `/detect` 应答接口说明（v2，供 OLED 显示）

> 变更：只增加字段，**原有字段含义与取值完全不变**。
> 服务器已上线，固件按本文对接即可。
> 显示改版方案见 [`OLED显示改版方案-有人检测.md`](OLED显示改版方案-有人检测.md)。

---

## 一、新增字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `v` | int | 协议版本，当前 `2` |
| `person` | bool | 快速通道是否检出人体（**注意：不等同于"有人在"**） |
| `scene` | string | `person` / `empty` / `occluded` |
| `display` | string | 短码，直接给 OLED 用 |
| `display_cn` | string | 中文文案（UTF-8），调试用；字库不方便就忽略 |

### `scene` 三分类的含义

| 值 | 含义 | 判定依据 |
|---|---|---|
| `person` | 画面里有人 | YOLO 人体得分 ≥ 0.5 |
| `empty` | 空座位，真没人 | 没人 + 画面有正常纹理（边缘密度 ≥ 0.02） |
| `occluded` | **看不清**：手挡镜头 / 关灯 / 朝天花板 / 朝墙 | 没人 + 极暗（均值<45）或近乎无纹理（边缘密度<0.02） |

> `empty` 和 `occluded` 都满足 `person=false`，**必须看 `scene` 才能区分**。
> 实测：空教室边缘密度 0.089，手挡镜头 0.0059，朝天花板 0.0133。

---

## 二、`display` 短码表

| `display` | `display_cn` | 何时出现 |
|---|---|---|
| `AWAY` | 无人 | `scene=empty` |
| `DIM` | 看不清 | `scene=occluded` |
| `UP` | 端正 | 有人且端正 |
| `SL` | 低头 | 有人且判不端正（几何判定） |
| `LEAN` | 侧歪 | 慢通道判侧歪 |
| `PHONE` | 手机 | 慢通道判玩手机 |
| `DAZE` | 走神 | 慢通道判发呆 |
| `SLEEP` | 趴桌 | 慢通道判趴桌 |
| `??` | 未知 | 兜底（正常不会出现） |

**建议固件只认 `display`，不要自己去拼 `state` + `person`**：
`AWAY`/`DIM` 的判定逻辑（亮度、边缘密度）只在服务器侧，固件拿不到这些信息。

---

## 三、实际应答样例

### 1. 没人（空座位）

```json
{
  "v": 2,
  "action": "none",
  "upright": null,
  "state": "away",
  "source": "slow",
  "reason": "slow_path_ok:画面中没有人",
  "person": false,
  "scene": "empty",
  "display": "AWAY",
  "display_cn": "无人",
  "head_height": null,
  "shoulder_level": null
}
```

### 2. 看不清（镜头被挡 / 太暗）

```json
{
  "v": 2,
  "action": "none",
  "upright": null,
  "state": "unknown",
  "source": "fused",
  "reason": "cannot_determine",
  "person": false,
  "scene": "occluded",
  "display": "DIM",
  "display_cn": "看不清",
  "head_height": null,
  "shoulder_level": null
}
```

### 3. 有人且端正

```json
{
  "v": 2,
  "action": "none",
  "upright": true,
  "state": "upright",
  "source": "fast",
  "head_height": 0.5091,
  "shoulder_level": 0.0622,
  "reason": "fast_path_ok",
  "person": true,
  "scene": "person",
  "display": "UP",
  "display_cn": "端正"
}
```

### 4. 有人但不端正（会触发吹风）

```json
{
  "v": 2,
  "action": "blow",
  "upright": false,
  "state": "slouch",
  "source": "fast",
  "head_height": -0.1035,
  "shoulder_level": 0.0467,
  "reason": "fast_path_bad",
  "person": true,
  "scene": "person",
  "display": "SL",
  "display_cn": "低头"
}
```

---

## 四、固件侧注意点

1. **`head_height` / `shoulder_level` 在没人时是 `null`**，解析要容错，别当数字用。
2. **`upright` 有三个取值**：`true` / `false` / `null`。`null` = 无法判定，不要当成 false 处理。
3. **`action` 仍只有 `blow` / `none`**，吹风逻辑未变（连续 2 帧不端正 + 60 秒去重）。
4. **`state` 可能滞后**：它来自慢通道缓存（TTL 45 秒）。要实时性就只看 `display`，
   它优先使用场景判定（没人/看不清是即时算的）。
5. 没人时服务器返回的 `display=AWAY` 建议 OLED 显示"无人"，同时**不要吹风**（`action` 会是 `none`）。

---

## 五、当前实现的边界

| 情况 | 现在的结果 | 说明 |
|---|---|---|
| 空教室 | `AWAY` | ✅ |
| 关灯 | `DIM` | ✅ 均值 < 45 |
| 手挡镜头 | `DIM` | ✅ 边缘密度 < 0.02 |
| 朝天花板 | `DIM` | ✅ |
| 朝白墙 | `DIM` | ✅ 近乎无纹理 |
| 人坐得很远但画面有条纹背景 | `AWAY` | ⚠️ 可能误判为无人（YOLO 认不出小目标时） |
| 画面正常但 YOLO 偶发漏检单帧 | `AWAY` | ⚠️ 单帧抖动，建议固件对 `AWAY` 做 2 帧确认 |

最后一条建议：**OLED 对 `AWAY`/`DIM` 也做 2 帧去抖**，避免 YOLO 偶发单帧漏检导致屏幕闪烁。
