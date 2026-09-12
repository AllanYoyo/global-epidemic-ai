---
name: epidemic-extraction
description: 把 data/raw/ 中的原始疫情情报抽取为标准疫情事件(JSON)并入库。当需要把网页正文、搜索结果、官方通报"整理成结构化疫情事件""建疫情事件库"时使用。使用 prompts/extraction.md 提示词抽取,经 scripts/normalize.py 校验后写入 data/events/ 与 SQLite。
---

# epidemic-extraction · 事件抽取

## 目标

把 `global-epidemic-search` 产出的原始情报,变成字段完整、可去重、可核验的标准"疫情事件"。

## 输入

- `data/raw/<日期>/`(raw-findings.md、search-log.md、存档文件)。

## 步骤

1. 读取 `prompts/extraction.md` 作为抽取提示词;字段权威定义见 `docs/event-schema.md`。
2. **逐源抽取**:对每份存档/每条情报,按提示词产出事件 JSON。一条通报含多起疫情时拆成多条;同一事件多来源时合并为一条,其余来源放入 `cross_sources`。
3. 汇总为 JSON 数组,存到 `data/raw/<日期>/extracted-events.json`。
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
- 与疫情无关的政策/市场新闻不入事件库。

## 输出

- `data/events/<日期>/<event_id>.json`
- SQLite `events` 表(路径:环境变量 `EPIDEMIC_DB`,默认 `database/epidemic.db`)

→ 全部新事件初始 `verification_status=unverified`,交给 `epidemic-verification`。
