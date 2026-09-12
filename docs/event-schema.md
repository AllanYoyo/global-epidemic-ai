# 疫情事件数据模型(Event Schema)

> 本模型是五个 Skill 与 `scripts/` 之间的"合同"。
> 代码唯一权威实现:`scripts/normalize.py`;本页是人类可读版。
> 任何一方改字段,必须同步:本文档、`normalize.py`、`prompts/extraction.md`。

## 字段表

| 字段 | 类型 | 必填 | 说明 | 示例 |
|---|---|---|---|---|
| event_id | string | 自动 | sha1(病名EN\|国名EN\|event_date\|region) 前 12 位;去重与更新的主键 | `a1b2c3d4e5f6` |
| disease_name_cn | string | ✅ | 病害中文名 | 非洲猪瘟 |
| disease_name_en | string | ✅ | 病害英文名 | African Swine Fever |
| category | string | ✅ | `animal` / `plant` | animal |
| pathogen | string | – | 病原(血清型/小种/株型) | H5N1 高致病性禽流感病毒 |
| host_species | string[] | – | 宿主动物 / 受害作物 | `["家猪","野猪"]` |
| country_cn / country_en | string | ✅ | 国家(中/英) | 德国 / Germany |
| region | string | – | 一级行政区(州/省) | Brandenburg |
| location_detail | string | – | 更具体的位置 | – |
| latitude / longitude | number | – | 有则填,用于地图与邻近计算 | – |
| event_date | string | ✅ | 发生/报告日期;粒度不足自动补齐并写 date_precision | 2026-09-10 |
| date_precision | string | 自动 | day / month / year | day |
| report_date | string | – | 官方发布日期 | 2026-09-12 |
| quantity | object | – | 动物: susceptible/cases/deaths/killed_or_disposed;植物: affected_area(带单位)/destroyed | `{"cases":35}` |
| spread_status | string | – | 新发 / 持续 / 已控制 / 不明 | 新发 |
| source | object | ✅ | `tier`(1-3) / `name` / `url` / `publish_date` / `quote`(≤80 字原文) | 见下 |
| cross_sources | object[] | – | 其他独立来源,结构同 source | – |
| verification_status | string | 自动 | verified / single_source / unverified / false_positive / merged | unverified |
| verification_notes | string | – | 核验结论一句话 | – |
| checked_urls | string[] | – | 核验时查过的链接(含无效线索,留轨迹) | – |
| china_risk | object | 研判后 | `{level, score, focus, rationale, trade_relevance, existing_gacc_measures, dimension_scores}` | 见下 |
| summary_cn | string | – | 一句话中文摘要(≤60 字) | – |
| raw_excerpt | string | – | 原文关键段落 | – |
| first_seen / updated_at | datetime | 自动 | 首次入库 / 最近更新 | – |
| merged_into | string | – | (重复事件)指向保留事件的 event_id | – |

## 状态流转

```text
raw(原始情报)
  → unverified ──(epidemic-verification)──→ verified / single_source / false_positive
  └────────(deduplicate)──────────────→ merged(merged_into 指向保留事件)
verified ──(china-risk-analysis)──→ 写入 china_risk(focus: 立即关注 / 持续观察 / 常规记录)
```

## 设计原则

1. **可溯源**:每条事件必须能通过 `source.url` 回到原始出处;摘不出原文引用的情报不进入事件库。
2. **不编造**:抽取时缺什么填 `null`,绝不推测;贸易背景没有就如实写"背景资料未提及"。
3. **幂等**:`event_id` 稳定,重复入库即更新;修改已有事件一律用 `normalize.py --update`(合并写入,保留 china_risk 等已写入字段)。
