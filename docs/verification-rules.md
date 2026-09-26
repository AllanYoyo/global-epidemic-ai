# 政策核验决策规则

> 配套 `docs/event-schema.md` 的 `verification_status` 字段与 `skills/policy-verification` 技能。
> 来源分级定义见 `config/sources.yaml`:**Tier 1** 官方(政府公报/法规库/国际组织正式发布)、
> **Tier 2** 专业(行业机构、专业媒体转述)、**Tier 3** 一般媒体(仅线索)。

## 组合判定表

| 场景 | 判定 | 说明 |
|---|---|---|
| 1× Tier1 官方原文, 关键字段一致 | `verified` | 政府公报、官方公告 PDF、法规数据库原文是充分条件 |
| 1× Tier1 + ≥1× Tier2 交叉一致 | `verified` | Tier1 已足够, 交叉源只作增强记录进 `cross_sources` |
| 1× Tier1, 但与其他来源关键字段冲突 | `single_source` + notes | 生效日期/适用范围/商品清单矛盾时降级, 冲突写入 `verification_notes` |
| ≥2× Tier2 相互一致, 无 Tier1 | `single_source` | 专业机构一致转述; 补到 Tier1 原文后升级 `verified` |
| 1× Tier2 单一来源 | `unverified` | 继续检索官方原文 |
| 仅 Tier3 媒体/自媒体传播 | `unverified` | 按线索处理, 不得进入影响研判 |
| Tier1 官方明确否认或撤回 | `false_positive` | 必须引用否认原文(url + quote) |
| 线索与官方原文实质矛盾(如措施不存在) | `false_positive` | 以官方原文为准 |
| 可疑但无官方否认(数据异常、日期错乱) | `unverified` | 不臆断误报, 留待核验 |
| 同一政策被重复抽取(另一 event_id) | `merged` | 保留信息最全的一条, 其余并入 |

## 硬性约束

1. **`false_positive` 必须有 Tier1 否认或矛盾原文佐证**,禁止因"找不到出处"直接标误报。
2. **`verified` 必须满足**: `source.url` 可回溯官方原文 + `source.quote`(≤120 字) + `checked_urls` 非空。
3. **升级路径**: `single_source` → 补 Tier1 原文 → `verified`,用 `--update`/`--replace` 写回并在 `verification_notes` 记录依据。
4. **ePing 等 SPS 通报按 `verified` 收录**(通报本身即官方文件),但 `policy_status` 如实标注草案/未生效,研判时不得当已生效处理。
5. `unverified` 记录不进入影响研判(`risk.py --list-pending` 只列 verified),但保留在台账待核实栏。

## 追溯要求

每条非 `unverified` 记录的核验结论都应能回答:**看了哪些 URL(`checked_urls`)、引用了哪句原文
(`source.quote`)、为什么判成这个状态(`verification_notes`)**。组合判定拿不准时保守降级
(`verified` → `single_source`),并在 notes 说明疑点。
