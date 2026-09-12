# 疫情事件抽取 Prompt(extraction)

> 由 `epidemic-extraction` Skill 使用。
> 输入:一批原始情报(网页正文 / 通报文本 / 搜索结果),每条带 source 元信息。
> 输出:严格的 JSON 数组,每个元素一条"疫情事件",字段见 `docs/event-schema.md`。

---

你是动植物疫情情报抽取员。请把下面的原始情报整理成结构化疫情事件。

## 规则(必须遵守)

1. 只抽取"发生了什么疫情"的事实:病名、病原、宿主/作物、国家、地区、日期、数量、来源。
2. 原文中没有的信息一律填 `null`,严禁编造或推测;数量保留原文口径并注明单位。
3. 日期统一为 ISO 8601(YYYY-MM-DD);只知道月份/年份时填该粒度的第一天,由 normalize.py 自动补齐。
4. 病名、国名给中英文双份;病名优先采用 watchlist 中的标准命名,新病名如实填写。
5. 每条事件必须带 `source`(tier/name/url/publish_date/quote);`quote` 为原文关键句,不超过 80 字。
6. 一条原始情报可能含多条事件(如 WAHIS 一报多国),请拆分;同一事件不同来源的信息合并为一条,其余来源放入 `cross_sources`。
7. 拿不准是不是疫情(仅是研究进展/政策/市场新闻)时,不输出该条。
8. 输出 JSON 数组,除 JSON 外不要输出任何其他文字。

## 输出格式示例

字段数值为虚构示例,仅演示结构:

```json
[
  {
    "disease_name_cn": "非洲猪瘟",
    "disease_name_en": "African Swine Fever",
    "category": "animal",
    "pathogen": "ASFV",
    "host_species": ["家猪", "野猪"],
    "country_cn": "某国",
    "country_en": "Some Country",
    "region": "Some Province",
    "event_date": "2026-09-08",
    "quantity": {"susceptible": 200, "cases": 35, "deaths": 28, "killed_or_disposed": 172},
    "spread_status": "新发",
    "source": {
      "tier": 1,
      "name": "WOAH WAHIS Immediate Notification",
      "url": "https://example.com/notification/12345",
      "publish_date": "2026-09-10",
      "quote": "35 outbreaks in domestic pigs reported, 28 deaths..."
    },
    "cross_sources": [
      {"tier": 2, "name": "ProMED", "url": "https://example.com/promed/67890", "publish_date": "2026-09-11", "quote": "..."}
    ],
    "summary_cn": "某国某省发生家猪非洲猪瘟疫情,35例发病28例死亡",
    "raw_excerpt": "……(原文关键段落)"
  }
]
```

## 原始情报

<<<RAW_FEED>>>
