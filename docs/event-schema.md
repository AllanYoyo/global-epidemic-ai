# 政府动植物检疫政策变化数据模型

> 本模型是政策监测 Skill 与 `scripts/` 之间的合同。
> 代码唯一权威实现:`scripts/normalize.py`;本页是人类可读版。
> 本分支生产数据只允许 `record_type=policy`;未来如重新建设疫情检测,应另建数据合同与管线。

## 记录范围

只收录**外国政府或国际组织发布的动植物检疫/进出口管控政策变化**,主题包括:
进出口暂停/禁止/恢复/放宽、检疫要求与进口卫生标准修订、双边进口议定书更新、SPS 通报、
区域化与等效性认可、企业/口岸/名单准入、进口前通报制度、个体标识要求(微芯片/腿环)、
动物源性生物制品与生物材料(细胞系/牛血清/诊断试剂盒)、病原微生物目录与实验活动审批、
兽药与违禁物质管控、食品/饲料添加剂与原料准入、植物检疫(种子/种苗/有害生物名录/新生物体)、
移动管控、免疫/扑杀政策、国际组织(WOAH/FAO/GPP-TAD/ECTAD/IPPC/EPPO)行动呼吁等。
中国海关总署不作为政策记录采集源,只在政策影响研判中按需作为背景。

## 公共字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| event_id | string | 自动 | sha1(政策国EN\|政策领域\|动作\|关联对象\|生效日期) 前 12 位;同一政策修订按主键幂等更新 |
| record_type | string | 自动 | 固定为 `policy` |
| category | string | 自动 | 固定为 `policy`;具体领域使用 `policy_domain` |
| country_cn / country_en | string | ✅ | 发布/适用政策的国家或地区 |
| region | string | – | 省州/保护区/口岸范围 |
| event_date | string | ✅ | 公告/生效日期;政策优先使用 `effective_date` |
| effective_date / effective_until | string | – | 生效日/有效期截止日 |
| source | object | ✅ | tier/name/url/publish_date/quote; quote 不超过 120 字 |
| cross_sources | object[] | – | 其他独立来源 |
| verification_status | string | 自动 | verified / single_source / unverified / false_positive / merged |
| verification_notes | string | – | 官方核验结论 |
| checked_urls | string[] | – | 核验轨迹 |
| first_seen / updated_at | datetime | 自动 | 首次入库/最近更新; update 不刷新 first_seen |
| summary_cn | string | – | 政策变化一句话摘要 |
| raw_excerpt | string | – | 原文关键段落 |

## 政策内容字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| title_cn / title_en | string | ✅至少其一 | 政策动作标题 |
| action_type | string | – | 收紧 / 放松 / 调整 / 恢复 |
| policy_status | string | – | 草案 / 已发布未生效 / 已生效 / 已解除 / 不明 |
| policy_domain | string | – | animal / plant / both / trade / measures;按政策的主要对象归类 |
| prev_action | string | – | 此前政策状态 |
| issuer_cn / issuer_en | string | – | 发布机构 |
| target_countries | string[] | – | 涉及国家/地区 |
| products | string[] | – | 受影响商品 |
| disease_name_cn / disease_name_en | string | – | 关联病害,可为空 |
| scope | string | – | 适用地区/企业/口岸 |
| legal_basis | string | – | 公告文号/法规编号/SPS 通报号 |

## 政策影响研判字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| impact_type | string | 研判后 | 约束 / 机会 / 中性 |
| impact_level | string | 研判后 | 高影响 / 中影响 / 低影响;不是病原风险 |
| china_relevance | string | 研判后 | 直接涉及中国 / 间接影响 / 暂无明显关联 |
| recommended_action | string | 研判后 | 核查准入、跟踪法规、提醒企业、常规记录等 |
| impact_score | number | 研判后 | 0–5 影响分 |
| impact_focus | string | 研判后 | 立即关注 / 持续观察 / 常规记录 |
| impact_rationale | string | 研判后 | 事实依据 |
| dimension_scores | object | 研判后 | trade/biosecurity/response/alignment 四维分数 |

## 状态流转

```text
raw(政策原文)
  → unverified ──(官方核验)──→ verified / single_source / false_positive
verified ──(政策影响研判)──→ 写入 impact_type / impact_level / china_relevance / recommended_action
```

## 设计原则

1. **可溯源**:政策必须能通过 `source.url` 回到外国政府/国际组织原文。
2. **不编造**:原文没有的商品、日期、影响和建议动作填 null 或“未注明”。
3. **政策优先**:生产入口、网页、Word、Excel、推送只处理政策变化。
4. **幂等**:政策主键稳定,重复入库更新原记录并保留 first_seen。
