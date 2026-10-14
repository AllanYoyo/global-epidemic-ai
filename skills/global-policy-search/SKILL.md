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
  - `sources`:源清单, 用 `track` 字段区分是否进入本档主动扫描:
    - `track: policy` —— 外国政府/国际组织的政策动作源, **policy 档只扫这些**;
    - `track: outbreak` —— 疫情事件源(WAHIS / EMPRES-i / ProMED / 行业媒体等), 本分支不扫, 仅在影响研判时作背景;
    - `track: background` —— 中国侧与标准名录源(gacc / mara / tbtsps / foodmate 等), 只作背景, 不进采集台账;
  - `policy_watch_countries`:检索主干, 6 个档位——`priority_1`(对华进口来源地, 每日必扫)、
    `regional`(区域/国际机构措施: 欧盟 SANTE、WTO ePing、IPPC, 每日必扫)、
    `priority_2`(主要贸易伙伴联邦级措施, 每日必扫)、
    `border_neighbours`(陆路边境邻国, 传入路径优先)、`export_partners`(对华出口大户, 反向措施)、
    `state_level`(次国家/州级部门)。后三档按 `weekly_shards` 周轮转, 每条含 source_ids 与别名;
  - `weekly_shards`:ISO 星期(1=周一..7=周日) → 当天要扫的档位;每日必扫档不在其中列出;
  - `policy_keywords`:政策关键词词库(组织机制/商品生物材料/活动物/植物/疫病风险/证书流程/法规标准,每条 {cn, en, aliases[]});
  - `policy_queries`:按主题分组的检索模板。

> **2026-10 起 policy 档只扫 `track: policy` 的源**。疫情源在本分支几乎不产出政策动作,
> 却会占用检索预算(实测日新增 1-2 条, 远低于 ePing 通报量)。

> **国别主干按周轮转**:`priority_1` / `regional` / `priority_2` 每日必扫;
> 其余条目按 `shard` 字段指定的 ISO 星期扫描(条目级优先于档位级 `scan`),
> 高疫区邻国(俄/越/哈/蒙)显式写全周 `1 2 3 4 5 6 7` 即等于每日扫。
> 这样 7 天全覆盖, 单日检索量不随国别数线性膨胀。
> **排期变更后必须自检**:`weekly_shards` 生效日按当天 ISO 星期取当天条目,
> 并在 `search-log.md` 头部注明当天星期与实际扫描的条目, 便于复盘"某国这周没产出是漏扫还是真无变化"。
> 新增国别必须给 `shard`, 否则该国永远不会被扫到(仓库已有校验脚本历史教训)。

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

1. **定窗口**:确认时间窗(默认今天;常规运行只收最近 24-48 小时的新发布,首次运行放宽到 7 天)。运行标识为当天日期 `YYYY-MM-DD`,并记下窗口下限「今天-N 天」(N 与入库校验一致,默认 7):只有发布时间落在窗口内的条目才算"新信息"。
2. **按优先级组合检索**:先按当天 ISO 星期取应扫条目,再并入每日必扫档:
   - **每日必扫**:`priority_1` → `regional` → `priority_2`(含欧盟 SANTE 与 WTO ePing);
   - **当天轮转条目**:`border_neighbours` / `export_partners` / `state_level` 中 `shard` 含当天星期的条目,
     以及 `weekly_shards[当天].scan` 里的整档;
   - 周一附加扫 `sources.yaml` 中 `schedule: weekly` 的政策源;
   - **只扫 `track: policy` 的源**;`track: outbreak` / `background` 的源不主动扫(需要背景时再按需查);
   - **WTO ePing 与欧盟 SANTE SERS 是第一高产源, 每日必扫**(均归 `regional` 档):ePing 优先走 RSS 订阅确定性拉取,订阅不可用再回退 browse/search;通报自带发布日期与 G/SPS/N 文号,直接映射 `legal_basis` / `effective_date` / 评议期;
   - 每个国家/机构使用 aliases 和 source_ids 定位官方站内公告,再用 `policy_keywords` 主题词与动作词组合;
   - 用 `policy_queries.sps` 过一遍 WTO ePing 动植物 SPS 通报列表;
   - 用 `policy_queries.official` 检索国家官方检疫机构新闻页;
   - 用 `policy_queries.animals / plants / biologics / certification / product_access / regulation / country_sweep` 逐主题检索;
   - 用 `policy_queries.organisations` 跟踪 WOAH/FAO/GPP-TAD/ECTAD/IPPC/EPPO 的行动与呼吁;
   - 用 `policy_queries.disease_triggered` 作为疫情背景触发型补充,不让病害清单决定国家范围。
   - 在 `search-log.md` 头部注明当天 ISO 星期与实际扫描的条目列表。
3. **逐条存档**:
   - **先核发布日期再存档**:从页面 meta、日期栏或文首确认发布时间;早于窗口下限的是常年挂网的旧闻,不存档不入库,在 search-log.md 该行末尾标注「跳过-旧闻」;页面完全没有日期线索时先从正文/URL/站点列表页判断,仍无法确认的按旧闻处理并标注「跳过-日期不明」;
   - 网页:`python scripts/collect.py --url "<URL>" --title "<标题>"`(存到 `data/raw/<日期>/`);
   - 搜索摘要:写入 `data/raw/<日期>/search-log.md`(每行:国家 | 主题 | 动作 | 来源 | 标题 | URL | 摘要 | 命中查询)。
4. **抽取入库**:按 `prompts/policy-extraction.md` 抽取为 `record_type=policy` 的记录(字段见 `docs/event-schema.md`),抽取时在 <<<RAW_FEED>>> 头部第一行注明「窗口下限: YYYY-MM-DD」。汇总到 `data/raw/<日期>/extracted-policy.json` 后:

   ```bash
   python scripts/normalize.py --input data/raw/<日期>/extracted-policy.json --out data/events/<日期>/ --db --max-age-days <N 与窗口一致, 缺省 7>
   ```

   event_id 已存在时自动合并更新, 空值与默认状态不覆盖已完成的核验/研判结论。normalize.py 会按 `source.publish_date`(缺省 `event_date`)把窗口外的**新增**记录判为旧闻拒收;确属新线索被误拦时,先核实页面日期,再用 `--max-age-days` 放宽重放。

5. **对华影响研判**:对每条 verified 政策记录,检索对华贸易与海关现有措施背景,按 `prompts/policy-impact.md` 研判,用 risk.py 写回政策专属字段:

   ```bash
   python scripts/risk.py --event-id <event_id> --impact-type 约束 \
     --china-relevance 间接影响 --action "立即核查我国相关进口准入" \
     --rationale "..." --dim trade=4 --dim biosecurity=3,response=3,alignment=2
   ```

   四维齐全时 score/level/focus 由代码按加权合同(trade*0.35+biosecurity*0.30+response*0.25+alignment*0.10)推导;
   给出的 score/level/focus 与四维不一致会被拒绝写回。

   政策影响分级(不是病原风险):高影响/中影响/低影响;影响类型:约束/机会/中性;
   中国关联:直接涉及中国/间接影响/暂无明显关联。仅中高影响政策做完整贸易背景检索,低影响只做基础判断。

6. **汇总产出**:写 `data/raw/<日期>/policy-findings.md`:按优先级国家、政策主题、动作类型和影响等级分组,列出涉及国家、机构、商品/动物/植物、关联病害与对华影响要点。

## 红线

- 每条记录必须带 URL 与抓取时间;没有出处的动作线索不进入下一环节。
- **旧闻不入库**:发布时间(`source.publish_date`,缺省 `event_date`)必须落在时间窗内;检索撞到的历史公告页即使 URL 从未见过也不入库,只在 search-log.md 备注。回补历史需显式放宽 `--max-age-days`,不得关掉校验当日常扫描用。
- **动作必须有原文依据**:"疑似将暂停""市场传闻"不入库;仅重申既有措施的公告不收。
- `action_type` 判定拿不准时保守记"调整",并在 summary_cn 说明,不臆断收紧/放松。
- Tier 3 媒体的政策消息只作线索,必须回溯官方公告原文后才可入库。
- 影响研判禁止编造贸易数据;海关现有措施找不到就写"背景资料未提及"。
- 国际组织呼吁(WOAH/FAO)本身收录为"行动呼吁"主题,不是我国必须响应的措施;不得据此臆断我国政策。

## 输出

- `data/raw/<日期>/`:policy-findings.md、search-log.md、存档文件;
- `data/events/` + SQLite:外国政府动植物检疫政策变化记录;
- 日报:`python scripts/report.py --date <YYYY-MM-DD> --excel`。
