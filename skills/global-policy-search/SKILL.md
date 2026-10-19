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
  - `policy_keywords`:政策关键词词库(组织机制/商品生物材料/活动物/植物/疫病风险/证书流程/法规标准/输华议定书/文书类型,每条 {cn, en, aliases[]});
  - `policy_queries`:按主题分组的检索模板。

> **2026-10 起 policy 档只扫 `track: policy` 的源**。疫情源在本分支几乎不产出政策动作,
> 却会占用检索预算(实测日新增 1-2 条, 远低于 ePing 通报量)。

> **检索通道实测排序(2026-10-15 依样例回溯, 按单次命中率; 2026-10-18 调整: 移除 tbtsps.cn)**:
> 1. **中文 SPS/TBT 通报平台**(省级 TBT 咨询中心 + 商务部)—— 2026-10-15 测时
>    含 tbtsps.cn(海关总署标法中心)日产量最高, 2026-10-18 撤除该源(见下方红线),
>    现仅余 5 个省级/商务部站: 转载以 WTO 通报译文为主, "中国自身措施"占比小;
>    单次可返回带**通报号 + 生效日期 + 评议期**的中文规范标题记录, 可直接入库。
> 2. **WTO ePing** —— 第一权威通道, 通报自带文号与日期, 但需按成员/产品过滤。
> 3. **中文输华议定书检索** —— 外国输华议定书**不进 ePing**, 必须用
>    `"输华" + 检疫 + 卫生要求 + 议定书` 单独扫; 这类是对华影响最直接的动作, 样例中占比很高。
> 4. **官方域直击**(`site:{official_domain}`) —— 必须用**短英文术语**
>    (`import requirements` / `quarantine` / `resolution`), 不要拼长自然语言句子。

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
- **输华双边准入**:外国与我国签署的输华检疫议定书及其修订/生效(西班牙饲用水生动物蛋白、乌拉圭牛黄、泰国食用水生动物、巴基斯坦干坚果、中俄活体驯鹿等)。这类**通常不进 WTO ePing**, 且常挂在对方商务部/外交部而非检疫机构, 必须用中文专扫; 它是对华影响最直接的一类动作;
- **区域统一立法主体**:除欧盟(SANTE/EUR-Lex)外, **欧亚经济委员会(EEC/EAEU)** 同构——措施由 EEC 理事会/委员会以决定草案发布,由成员国(常为吉尔吉斯斯坦/亚美尼亚)代为向 WTO 通报,**按通报成员找会漏**, 须按措施主体找; 正文在 `regulation.eaeunion.org`;
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
   - **WTO ePing 与欧盟 SANTE SERS 是第一高产源, 每日必扫**(均归 `regional` 档):ePing 平台主域为
     `epingalert.org`(旧 `eping.wto.org` 会跳转), 通报自带发布日期与 G/SPS/N 文号,直接映射
     `legal_basis` / `effective_date` / 评议期。**2026-10 实测提醒**:ePing 前端 API 基址虽为
     `https://epingalert.org/api`, 但 `/api/v1/notifications` 等常见 REST 端点直连返回 404
     (前端走 POST + 命名空间路径), **不要依赖未验证的端点**; 走 browse 检索页, 或经中文聚合站
     回溯 `members.wto.org` 原始通报 PDF(后者更稳, 且是 tier:1 可存档的官方域);
   - 每个国家/机构使用 aliases 和 source_ids 定位官方站内公告,再用 `policy_keywords` 主题词与动作词组合;
   - **先跑中文聚合站与输华议定书两条通道**(实测高产, 见上"检索通道实测排序"), 再回到官方域核验;
   - **按文书类型横切**(`policy_queries.instruments`): 样例标题几乎全部是"某国发布/修订/批准某类文书",
     只按"国别 × 商品"纵切会漏掉不知道近期有无动作的国家——横切覆盖面更广;
   - 用 `policy_queries.sps` 过一遍 WTO ePing 动植物 SPS 通报列表;
   - 用 `policy_queries.official` 检索国家官方检疫机构新闻页;
   - 用 `policy_queries.animals / plants / biologics / certification / product_access / regulation / country_sweep` 逐主题检索;
   - 用 `policy_queries.organisations` 跟踪 WOAH/FAO/GPP-TAD/ECTAD/IPPC/EPPO 的行动与呼吁;
   - 用 `policy_queries.cn_sps_aggregator` 过中文通报聚合站; 用 `policy_queries.cn_protocol` 专扫输华议定书;
   - 用 `policy_queries.official_domain` 做官方域直击时, **术语要短**(`import requirements` / `resolution`),
     长自然语言句在该通道实测 0 命中;
   - 用 `policy_queries.disease_triggered` 作为疫情背景触发型补充,不让病害清单决定国家范围。
   - 在 `search-log.md` 头部注明当天 ISO 星期与实际扫描的条目列表。
3. **逐条存档**:
   - **先核发布日期再存档**:从页面 meta、日期栏或文首确认发布时间;早于窗口下限的是常年挂网的旧闻,不存档不入库,在 search-log.md 该行末尾标注「跳过-旧闻」;页面完全没有日期线索时先从正文/URL/站点列表页判断,仍无法确认的按旧闻处理并标注「跳过-日期不明」;
   - **目录页/历史归档页(2026-10-19 强制)**:`config/sources.yaml` 中 `list_mode: aggregate`
     的源(如 ru-fsvps /vse-soobshcheniya 全站通知汇总页)整体不可作为单条政策的来源 URL。
     目录页自身没有页面级发布日期, 把页面内最旧/最新条目日期填进 `source.publish_date`
     是绕过 7 天窗口的典型错误。处理:
     1. 不对目录页本身 `collect.py` 存档; 在 search-log.md 标「列表页-不直接存档」;
     2. 对页面内每个列表项, **点入其原 URL**, 抓取条目页, 按"单条政策"流程处理;
     3. 整页无新条目或无独立 URL 时, 标「列表页-无窗口内新条目」并结束;
     4. 抽取阶段见 `prompts/policy-extraction.md` 页面类型判别段; 入库端
        `normalize.py` 的 `LIST_URL_PATH_FRAGMENTS` 是兜底黑名单(命中即拒收)。
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

- **官方域名硬约束**（2026-10 起）:`policy_queries` 的通用检索式(`"{country}" suspends imports ...`)会命中第三方站点——行业媒体、他国贸易门户转述、商业检疫数据库。2026-10 实测 19 条入库记录 100% `unverified`,逾半数来源是 `freyrsolutions.com`/`qpests.insect-genome.com`/`thebrief.com.na`/`www.evs.ee`/`bangladeshtradeportal.gov.bd`/`agrinfo.eu` 这类站点,却被标了 `tier: 1`。因此:
  - 某国政策**必须落回该国官方域名**(政府 `.gov`/`.gub`/`.go.*`、EUR-Lex、ePing 等),或回落到 `config/sources.yaml` 里该国 `source_ids` 对应的官方入口;
  - 第三方站点命中**只能写进 `search-log.md` 当线索**,不 `collect.py` 存档、不入库;确认其为官方原文转载后再回溯;
  - `normalize.py` 会按 `OFFICIAL_DOMAIN_SUFFIXES` 强校验:`tier: 1` 配非官方域名直接拒收;命中第三方黑名单的记录**整条拒收不入库**(tier 强制降为 3 且不得判 `verified`);该国官方域确实未收录时,在 `source` 里显式加 `official: 1` 说明(入库端会自动写 `source.official_note` 留痕, 待人工复核)。
- **中文 SPS/TBT 通报平台 tier 1 准入(2026-10-15 放宽, 2026-10-18 改: 仅省级 TBT + 商务部)**:
  仅省级 TBT 通报咨询中心(`gdtbt.org.cn` / `hntbt.org.cn` / `gxzl.org.cn` / `wzisps.org.cn`)
  与商务部 WTO/FTA 咨询网(`chinawto.mofcom.gov.cn`)作 tier 1, 可直接支撑 verified。
  **`tbtsps.cn`(海关总署标法中心)2026-10-18 撤除**: 同时大量转载中国海关总署公告
  与商务部贸易环境专刊, 混进政策台账时绕过 AGENTS.md "中国自身措施不入台账"的红线;
  撤出 OFFICIAL_DOMAIN_SUFFIXES 并加进 THIRD_PARTY_HINTS 双重保险。
  使用要求(5 个站):
  - **字段取原文**:`source.title` 填 G/SPS/N 文号、`publish_date` 填**通报发布日**(非转载日)、
    `quote` 取通报原文关键段(≤120 字); **不得用平台自加的"分析提醒/企业提示"段落当引文**——
    那是平台编辑加工, 不是原文, 拿来会污染可溯源链;
  - **中国自身措施不入台账**:平台转载的中华人民共和国海关总署公告、商务部贸易环境专刊等
    涉及我国自身措施的内容, 按 AGENTS.md 约定仅作影响研判背景, 不采为政策记录;
  - 若某条记录源 URL 落在撤除的 `tbtsps.cn` 上, normalize 会强制降 tier 3 并报错
    "不在官方域名库内", 不允许其被判 verified。
- **欧盟议题不按成员国拆分**:欧盟动植物检疫为层面统一立法(植物健康 Reg (EU) 2016/2031、动物健康 Reg (EU) 2019/2131),紧急措施由 SANTE 以实施条例/SERS 发布。搜欧盟措施只查 `eu-sante-sers` 与 EUR-Lex,**不要**按法国/德国/意大利/西班牙/荷兰/匈牙利逐国检索——那是同一议题的重复劳动,成员国源已降为 `track: outbreak`。
- 每条记录必须带 URL 与抓取时间;没有出处的动作线索不进入下一环节。
- **旧闻不入库**:发布时间(`source.publish_date`,缺省 `event_date`)必须落在时间窗内;检索撞到的历史公告页即使 URL 从未见过也不入库,只在 search-log.md 备注。回补历史需显式放宽 `--max-age-days`,不得关掉校验当日常扫描用。
- **目录页/历史归档页不可作为单条政策来源(2026-10-19 强制)**:`source.url`
  命中已知列表页模式(`vse-soobshcheniya` / `all-notifications` / `archive` / `news-list`
  / `event-list` / `notification-list` 等,以 `normalize.py` 的 `LIST_URL_PATH_FRAGMENTS`
  为准)时, 整页直接拒收, 须点入目录页内具体条目原 URL 后再用 `source.url`。
  `source.publish_date` 必须是**该 URL 自己的页面级发布日期**(HTML meta / RSS
  pubDate / 文首日期 / 文末修订日), 严禁用今天(抓取日)或目录页内某条目日期冒充——
  这是绕过 7 天窗口的常见手法, 复现案例 2026-10-19 fsvps 目录页误把 2026-07-31
  历史条目当作新政策入库即因此而来。
- **动作必须有原文依据**:"疑似将暂停""市场传闻"不入库;仅重申既有措施的公告不收。
- `action_type` 判定拿不准时保守记"调整",并在 summary_cn 说明,不臆断收紧/放松。
- Tier 3 媒体的政策消息只作线索,必须回溯官方公告原文后才可入库。
- 影响研判禁止编造贸易数据;海关现有措施找不到就写"背景资料未提及"。
- 国际组织呼吁(WOAH/FAO)本身收录为"行动呼吁"主题,不是我国必须响应的措施;不得据此臆断我国政策。

## 输出

- `data/raw/<日期>/`:policy-findings.md、search-log.md、存档文件;
- `data/events/` + SQLite:外国政府动植物检疫政策变化记录;
- 日报:`python scripts/report.py --date <YYYY-MM-DD> --excel`。

> **日期口径**:`--date` 必须传运行当天的**东八区日期**。`scripts/run_scan.sh` 会把
> `$SCAN_DATE` 写进 prompt 并显式传给 `report.py` / `push_report.py`;手工调用时用
> `python -c "import sys;sys.path.insert(0,'scripts');from normalize import biz_today;print(biz_today())"`
> 取当前业务日期, **不要**用 `date` 命令或自己算——生产 VPS 系统时区是 `Etc/UTC`,
> 直接取 UTC 日期会让北京时间早间跑批错一天。

## 评审核闸门(2026-10-19 引入)

用户在某渠道(微信/钉钉/邮箱)提出测试类请求时, 反复强调"反馈必须先在原渠道完成再扩散到其它渠道"。
本档在 Hermes 调 `push_report.py` 时, 应按用户在哪个渠道提问来设置 `REVIEW_CHANNEL`:

- 用户在**企业微信**里发起的扫描/测试请求 → 该次 `push_report.py` 调用前设
  `REVIEW_CHANNEL=wechat`, 首跑只发微信, 写 `data/reports/.review-pending/<日期>.flag`。
  用户在微信端确认后, 再调 `push_report.py --date X --review-approved` 把钉钉/邮箱发完。
- 同理 `REVIEW_CHANNEL=dingtalk` / `REVIEW_CHANNEL=mail`。
- 阶段 2 的审阅渠道**以 `.review-pending` 标记里记录的渠道为准**, `REVIEW_CHANNEL` 只作兜底:
  换会话/换机器执行忘了带环境变量也不会让审阅渠道重复收报(2026-10-19 修复)。
- 不设 `REVIEW_CHANNEL` 时所有渠道一次性发, 行为与之前一致, 适用于 cron 全量推送。
- `--message` 自定义文本不走闸门(避免 cron fallback 的"待执行提醒"被卡)。

为什么不能只在 chat 里强调: push_report.py 旧版"配了哪个发哪个, 一发全发", 没有 review-gate
状态机, 无论 chat 怎么强调都无效; 本工作流必须靠 `REVIEW_CHANNEL` + `--review-approved`
落地。详见 `scripts/push_report.py` 模块 docstring。
