---
name: global-policy-search
description: 监测各国政府动植物检疫与进出口管控政策变化。当用户要求"追踪各国检疫政策""看看哪些国家暂停/恢复进出口""生成今日政策日报",或需要对华措施动向做监测时使用。基于 config/sources.yaml 的 policy_keywords 词库与 policy_queries 检索各国官方检疫机构、WTO ePing SPS 通报与贸易伙伴公告,把政策变化动作(record_type=policy)带 URL 存档、抽取入库、评估对华影响,产出政策变化日报。
---

# global-policy-search · 全球检疫政策变化监测

## 目标

为一个运行日(默认最近 24-48 小时,首次运行可放宽到 7 天)收集各国政府动植物检疫管控政策的变化动作,形成带出处的政策情报包,完成抽取、影响研判与日报。

本技能是本分支唯一的生产检索入口,只回答"各国政府对检疫与进出口做了什么"。只收录**政策动作**,不收集疫情事件本身。

## 输入

- `config/sources.yaml`:
  - `sources`:含 measures 覆盖的官方源清单;
  - `policy_watch_countries`:priority_1 / priority_2 / state_level 国家和官方 source_ids;
  - `policy_keywords`:政策关键词词库(组织机制/商品生物材料/活动物/植物/疫病风险/证书流程/法规标准,每条 {cn, en, aliases[]});
  - `policy_queries`:按主题分组的检索模板。

## 监测范围(什么算"政策变化")

**只监测外国政府与国际组织的政策变化,中国海关总署公告不作为政策记录采集** —— 中国口岸措施不进入本政策台账。

- **进出口管控**:暂停/禁止/恢复/放宽进口,新增检疫证书或注册要求,指定/取消口岸,企业名单增删;
- **检疫条件与协议修订**:第三国进出口检疫要求修订、双边进口议定书更新(如活鱼进口议定书)、进口卫生标准(IHS)修订;
- **SPS 官方通报**:WTO ePing 上各成员的 SPS 紧急/常规通报(G/SPS/N/*),含评议期与过渡期信息;
- **活动物与繁殖材料**:马科动物、雏鸡、种蛋、种禽、蜜蜂、活兔、水生活体、种用遗传物质、犬猫等的进口条件变化;标识要求(微芯片 ISO 11784、封闭腿环);
- **生物制品与病原微生物监管**:动物源性生物制品、细胞系、牛血清、病毒载体、诊断试剂盒的进出口要求;病原微生物目录修订、高致病性病原微生物实验活动审批;
- **兽药与违禁物质**:兽药注册与残留限量、克伦特罗/氯霉素等违禁物质管控措施;
- **植物检疫**:种子、种苗、组培苗、水果、木材、花卉的检疫要求;有害生物与转基因/新生物体名录变化;
- **国际组织行动**:WOAH/FAO 的行动呼吁、GPP-TAD/ECTAD 项目动态(预警信号,提示后续政策风险);
- **国内防疫管制**:封锁区/保护区划定,扑杀与疫苗接种政策,运输限制;
- **涉华间接影响**:他国对中国产品的准入变化(反向措施),以及他国措施可能引发的贸易转移。

## 步骤

1. **定窗口**:确认时间窗(默认今天)。运行标识为当天日期 `YYYY-MM-DD`。
2. **按优先级组合检索**:按 `policy_watch_countries.priority_1` → `priority_2` → `state_level` 顺序扫描,每个国家至少覆盖动物、植物、进出口/措施三个主题面:
   - 第一优先级国家每日扫描;第二优先级国家分批/每周扫描;美国州级通道以州名+州农业部门/州兽医官/植物监管官检索;
   - 每个国家使用 aliases 和 source_ids 定位官方站内公告,再用 `policy_keywords` 主题词与动作词组合;
   - 用 `policy_queries.sps` 过一遍 WTO ePing 动植物 SPS 通报列表;
   - 用 `policy_queries.official` 检索国家官方检疫机构新闻页;
   - 用 `policy_queries.animals / plants / biologics / certification / product_access / regulation / country_sweep` 逐主题检索;
   - 用 `policy_queries.organisations` 跟踪 WOAH/FAO/GPP-TAD/ECTAD/IPPC/EPPO 的行动与呼吁;
   - 用 `policy_queries.disease_triggered` 作为疫情背景触发型补充,不让病害清单决定国家范围。
3. **逐条存档**:
   - 网页:`python scripts/collect.py --url "<URL>" --title "<标题>"`(存到 `data/raw/<日期>/`);
   - 搜索摘要:写入 `data/raw/<日期>/search-log.md`(每行:国家 | 主题 | 动作 | 来源 | 标题 | URL | 摘要 | 命中查询)。
4. **抽取入库**:按 `prompts/policy-extraction.md` 抽取为 `record_type=policy` 的记录(字段见 `docs/event-schema.md`),汇总到 `data/raw/<日期>/extracted-policy.json` 后:

   ```bash
   python scripts/normalize.py --input data/raw/<日期>/extracted-policy.json --out data/events/<日期>/ --db
   ```

5. **对华影响研判**:对每条 verified 政策记录,检索对华贸易与海关现有措施背景,按 `prompts/policy-impact.md` 研判,用 risk.py 写回政策专属字段:

   ```bash
   python scripts/risk.py --event-id <event_id> --impact-type 约束 --impact-level 高影响 \
     --china-relevance 间接影响 --action "立即核查我国相关进口准入" \
     --rationale "..." --score 3.8 --focus 立即关注
   ```

   政策影响分级(不是病原风险):高影响/中影响/低影响;影响类型:约束/机会/中性;
   中国关联:直接涉及中国/间接影响/暂无明显关联。仅中高影响政策做完整贸易背景检索,低影响只做基础判断。

6. **汇总产出**:写 `data/raw/<日期>/policy-findings.md`:按优先级国家、政策主题、动作类型和影响等级分组,列出涉及国家、机构、商品/动物/植物、关联病害与对华影响要点。

## 红线

- 每条记录必须带 URL 与抓取时间;没有出处的动作线索不进入下一环节。
- **动作必须有原文依据**:"疑似将暂停""市场传闻"不入库;仅重申既有措施的公告不收。
- `action_type` 判定拿不准时保守记"调整",并在 summary_cn 说明,不臆断收紧/放松。
- Tier 3 媒体的政策消息只作线索,必须回溯官方公告原文后才可入库。
- 影响研判禁止编造贸易数据;海关现有措施找不到就写"背景资料未提及"。
- 国际组织呼吁(WOAH/FAO)本身收录为"行动呼吁"主题,不是我国必须响应的措施;不得据此臆断我国政策。

## 输出

- `data/raw/<日期>/`:policy-findings.md、search-log.md、存档文件;
- `data/events/` + SQLite:外国政府动植物检疫政策变化记录;
- 日报:`python scripts/report.py --date <YYYY-MM-DD> --excel`。
