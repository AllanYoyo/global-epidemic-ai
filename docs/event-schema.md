# 事件数据模型(Event Schema)

> 本模型是各 Skill 与 `scripts/` 之间的"合同"。
> 代码唯一权威实现:`scripts/normalize.py`;本页是人类可读版。
> 任何一方改字段,必须同步:本文档、`normalize.py`、`prompts/extraction.md` / `prompts/policy-extraction.md`。

## 两类记录(record_type)

| record_type | 含义 | 事件来源 | 专用 Prompt |
|---|---|---|---|
| `outbreak`(兼容) | 疫情事件:发生了什么病、在哪里、多大范围 | `global-epidemic-search` | `prompts/extraction.md` |
| `policy` | 政策变化:外国政府对动植物检疫/进出口管控做了什么动作(仅外国政府;中国海关总署公告不采集,由 china-risk-analysis 按需检索) | `global-policy-search`(policy 分支) | `prompts/policy-extraction.md` |

两类记录共用同一事件库、核验分级与 `china_risk` 研判(对政策记录含义为"对华影响"),按 `record_type` 区分。生产入口默认写入 `policy`;旧 JSON 缺少 `record_type` 时由 normalize.py 按病名/政策字段兼容推断。

## 字段表(公共)

| 字段 | 类型 | 必填 | 说明 | 示例 |
|---|---|---|---|---|
| event_id | string | 自动 | outbreak: sha1(病名EN\|国名EN\|event_date\|region) 前 12 位;policy: sha1("policy"\|国名EN\|policy_domain\|action_type\|标题) —— 同一政策修订入库即更新,不新建 | `a1b2c3d4e5f6` |
| record_type | string | 自动 | `outbreak` / `policy` | policy |
| category | string | ✅ | outbreak: `animal` / `plant`; policy: 标记 `policy`, 具体领域写 `policy_domain` | policy |
| country_cn / country_en | string | ✅ | 国家(中/英) | 德国 / Germany |
| region | string | – | 一级行政区(州/省) | Brandenburg |
| event_date | string | ✅ | 发生/生效/公告日期;粒度不足自动补齐并写 date_precision | 2026-09-10 |
| date_precision | string | 自动 | day / month / year | day |
| source | object | ✅ | `tier`(1-3) / `name` / `url` / `publish_date` / `quote`(政策记录≤120字, outbreak按原文关键句) | 见下 |
| cross_sources | object[] | – | 其他独立来源,结构同 source | – |
| verification_status | string | 自动 | verified / single_source / unverified / false_positive / merged | unverified |
| verification_notes | string | – | 核验结论一句话 | – |
| checked_urls | string[] | – | 核验时查过的链接(含无效线索,留轨迹) | – |
| china_risk | object | 研判后 | `{level, score, focus, rationale, trade_relevance, existing_gacc_measures, dimension_scores}` | 见下 |
| summary_cn | string | – | 一句话中文摘要(≤60 字) | – |
| raw_excerpt | string | – | 原文关键段落 | – |
| first_seen / updated_at | datetime | 自动 | 首次入库 / 最近更新 | – |
| merged_into | string | – | (重复事件)指向保留事件的 event_id | – |

## 字段表(outbreak 疫情事件专用)

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| disease_name_cn / disease_name_en | string | ✅ | 病害中英文名(优先 watchlist 标准命名) |
| pathogen | string | – | 病原(血清型/小种/株型) |
| host_species | string[] | – | 宿主动物 / 受害作物 |
| location_detail | string | – | 更具体的位置 |
| latitude / longitude | number | – | 有则填,用于地图与邻近计算 |
| report_date | string | – | 官方发布日期 |
| quantity | object | – | 动物: susceptible/cases/deaths/killed_or_disposed;植物: affected_area(带单位)/destroyed |
| spread_status | string | – | 新发 / 持续 / 已控制 / 不明 |

## 字段表(policy 政策变化专用)

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| title_cn / title_en | string | ✅(至少其一) | 政策动作一句话标题 |
| action_type | string | – | 收紧(加严/新增限制) / 放松(取消/简化) / 调整(范围/程序变更) / 恢复(解除后重新允许) |
| policy_domain | string | – | animal / plant / both / trade / measures |
| prev_action | string | – | 该国该领域此前动作(原文提及才填) |
| products | string[] | – | 涉及商品/品类(HS 章节或品名,原文口径) |
| legal_basis | string | – | 公告文号 / 法规编号 / 通报编号(如 G/SPS/N/xxx) |
| effective_until | string | – | 有效期截止(ISO 8601) |
| scope | string | – | 适用地区/企业/口岸范围 |
| disease_name_cn / disease_name_en | string | – | 仅针对特定病害的政策才填 |

> 政策记录的病名可缺(不针对单一病害的政策),`country_cn/en` + `event_date` + `source` 仍必填;`title_cn/title_en/summary_cn` 至少其一。
> 政策记录不参与 `deduplicate.py` 聚合(同一条政策靠稳定 event_id 幂等更新)。

## 状态流转

```text
raw(原始情报)
  → unverified ──(epidemic-verification)──→ verified / single_source / false_positive
  └────────(deduplicate, 仅 outbreak)────→ merged(merged_into 指向保留事件)
verified ──(china-risk-analysis / policy 影响研判)──→ 写入 china_risk(focus: 立即关注 / 持续观察 / 常规记录)
```

## 设计原则

1. **可溯源**:每条事件必须能通过 `source.url` 回到原始出处;摘不出原文引用的情报不进入事件库。
2. **不编造**:抽取时缺什么填 `null`,绝不推测;贸易背景没有就如实写"背景资料未提及"。
3. **幂等**:`event_id` 稳定,重复入库即更新;修改已有事件一律用 `normalize.py --update`(合并写入,保留 china_risk 等已写入字段)。
4. **一库两类**:疫情与政策变化共用存储与管线,日报与面板按 `record_type` 分流,避免两套数据模型漂移。
