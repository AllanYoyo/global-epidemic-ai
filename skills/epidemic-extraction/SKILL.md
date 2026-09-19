---
name: epidemic-extraction
description: 把 data/raw/ 中的原始情报抽取为标准政策记录(JSON)并入库。当需要把外国政府政策公告、SPS 通报、官方新闻"整理成结构化政策变化""建政策台账"时使用。默认使用 prompts/policy-extraction.md;旧疫情事件用 prompts/extraction.md 显式处理,经 scripts/normalize.py 校验后写入 data/events/ 与 SQLite。
---

# epidemic-extraction · 政策记录抽取

## 目标

默认把 `global-policy-search` 产出的外国政府政策原始情报,变成字段完整、可核验的标准 `record_type=policy` 政策变化记录;旧疫情事件仍可显式走原抽取 prompt。

## 输入

- `data/raw/<日期>/`(raw-findings.md、search-log.md、存档文件)。

## 步骤

1. 政策情报读取 `prompts/policy-extraction.md`;旧疫情情报才读取 `prompts/extraction.md`;字段权威定义见 `docs/event-schema.md`。
2. **逐源抽取**:对每份存档/每条公告,按提示词产出政策 JSON。一份公告含多项独立措施时拆成多条;同一政策多来源时合并为一条,其余来源放入 `cross_sources`。
3. 汇总为 JSON 数组,政策记录存到 `data/raw/<日期>/extracted-policy.json`。
4. **校验入库**:

   ```bash
   python scripts/normalize.py --input data/raw/<日期>/extracted-events.json --out data/events/<日期>/ --db
   ```

   校验失败会逐条列出错误原因——修正字段后重跑,不要绕过校验。
5. **修改/补充已有事件**一律加 `--update`(按 event_id 合并写入,保留 china_risk 等已写入字段):

   ```bash
   python scripts/normalize.py --input patch.json --update --db
   ```

## 红线

- 原文没有的字段填 `null`,禁止编造或"合理推测"(尤其数量、日期、地点)。
- 每条事件必须有 `source.url` + `quote`(原文关键句);摘不出关键句的情报退回侦察环节。
- 纯疫情通报、市场行情、研究进展不入政策台账;政策动作必须有外国政府/国际组织官方出处。
- 中国海关总署公告不作为政策记录采集;对华措施只在影响研判阶段按需补充。

## 输出

- `data/events/<日期>/<event_id>.json`
- SQLite `events` 表(路径:环境变量 `EPIDEMIC_DB`,默认 `database/epidemic.db`)

→ 全部新事件初始 `verification_status=unverified`,交给 `epidemic-verification`。
