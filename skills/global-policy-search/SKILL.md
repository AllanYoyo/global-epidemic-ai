---
name: global-policy-search
description: 监测各国政府动植物检疫与进出口管控政策变化。当用户要求"追踪各国检疫政策""看看哪些国家暂停/恢复进出口""生成今日政策日报",或需要对华措施动向做监测时使用。检索各国官方检疫机构、WTO ePing SPS 通报与贸易伙伴公告,把政策变化动作(record_type=policy)带 URL 存档、抽取入库、评估对华影响,产出政策变化日报。
---

# global-policy-search · 全球检疫政策变化监测

## 目标

为一个运行日(默认最近 24-48 小时,首次运行可放宽到 7 天)收集各国政府动植物检疫管控政策的变化动作,形成带出处的政策情报包,完成抽取、影响研判与日报。

**与 `global-epidemic-search` 的分工**:那里回答"发生了什么疫情";本技能回答"各国政府因此(或因其他考虑)对检疫与进出口做了什么"。政策分支只收录**动作**,不重复入库疫情本身。

## 输入

- `config/sources.yaml`:`sources`(含 measures 覆盖的源)、`policy_queries`(政策检索模板)、`watchlist`(病害清单,用于相关性判断)。

## 监测范围(什么算"政策变化")

**只监测外国政府的政策变化,中国海关总署公告不作为政策记录采集** —— 对华口岸措施属于 `china-risk-analysis` 的研判输入(按事件按需检索 `search_queries.china`),重复收集没有增量价值。

- **进出口管控**:外国政府暂停/禁止/恢复/放宽进口,新增检疫证书或注册要求,指定/取消口岸,企业名单增删;
- **SPS 官方通报**:WTO ePing 上各成员的 SPS 紧急通报与常规通报(G/SPS/N/*),尤其涉及动植物卫生的;
- **国内防疫管制**:封锁区/保护区划定,扑杀与疫苗接种政策,运输限制(影响供应与贸易流向);
- **涉华间接影响**:他国对中国产品的准入变化(反向措施),以及他国措施可能引发的贸易转移。

## 步骤

1. **定窗口**:确认时间窗(默认今天)。运行标识为当天日期 `YYYY-MM-DD`。
2. **逐源扫描**(全部为外国政府/国际组织渠道,不含中国海关总署):
   - 用 `policy_queries.sps` 过一遍 WTO ePing 动植物 SPS 通报列表;
   - 用 `policy_queries.official` 检索重点伙伴(美/欧/澳/新/巴西/俄/日/韩/东南亚)官方检疫机构新闻页的管控公告;
   - 对昨日事件库中的高关注疫情,用 `policy_queries.reactive` 检索各国"因疫停进口"的连带反应。
3. **逐条存档**(与侦察技能同规则):
   - 网页:`python scripts/collect.py --url "<URL>" --title "<标题>"`(存到 `data/raw/<日期>/`);
   - 搜索摘要:写入 `data/raw/<日期>/search-log.md`(每行:国家 | 动作 | 来源 | 标题 | URL | 摘要 | 命中查询)。
4. **抽取入库**:按 `prompts/policy-extraction.md` 抽取为 `record_type=policy` 的记录(字段见 `docs/event-schema.md`),汇总到 `data/raw/<日期>/extracted-policy.json` 后:

   ```bash
   python scripts/normalize.py --input data/raw/<日期>/extracted-policy.json --out data/events/<日期>/ --db
   ```

5. **对华影响研判**:对每条 verified 政策记录,检索对华贸易与海关现有措施背景,按 `prompts/policy-impact.md` 研判,写回政策专属字段与兼容评分(复用 risk.py):

   ```bash
   python scripts/risk.py --event-id <event_id> --level high --score 3.8 --focus 立即关注 \
     --rationale "..." --trade "..." --gacc "..." \
     --impact-type 约束 --impact-level 高影响 --china-relevance 间接影响 --action "立即核查我国相关进口准入"
   ```

   政策影响分级(不是病原风险):高影响/中影响/低影响;影响类型:约束/机会/中性;
   中国关联:直接涉及中国/间接影响/暂无明显关联。仅中高影响政策做完整贸易背景检索,低影响只做基础判断。

6. **汇总产出**:写 `data/raw/<日期>/policy-findings.md`:本期动作数(按收紧/放松/调整/恢复分组)、涉及国家与商品、与我 watchlist 病害的关联、对华影响要点。

## 红线

- 每条记录必须带 URL 与抓取时间;没有出处的动作线索不进入下一环节。
- **动作必须有原文依据**:"疑似将暂停""市场传闻"不入库;仅重申既有措施的公告不收。
- `action_type` 判定拿不准时保守记"调整",并在 summary_cn 说明,不臆断收紧/放松。
- Tier 3 媒体的政策消息只作线索,必须回溯官方公告原文后才可入库。
- 影响研判禁止编造贸易数据;海关现有措施找不到就写"背景资料未提及"。

## 输出

- `data/raw/<日期>/`:policy-findings.md、search-log.md、存档文件;
- `data/events/` + SQLite:`record_type=policy` 的政策记录(与疫情事件同库,按 record_type 区分);
- 日报:`python scripts/report.py --policy --date <YYYY-MM-DD> --excel`。
