# AGENTS.md · 疫见全球（policy 分支）

外国政府/国际组织动植物检疫**政策变化**监测系统。数据合同只允许 `record_type=policy`（疫情事件管线已在本分支移除，勿重新引入）。中国海关总署公告**不作为政策采集源**，仅在影响研判时作背景。Agent 全流程（检索→抽取→核验→研判）由 Hermes 执行；`scripts/` 是确定性工具层。

## 生产链路（2026-10-25 P4 起：脚本开车，LLM 只当顾问；全链路目标 ≤10 分钟）

```
run_scan.sh policy (定时入口, 默认确定性管线)
  → scripts/policy_fetch.py       (零LLM取数 · urlwatch 内核: probe_channels 预探
                                    + 条件请求(ETag/304) + 关注面指纹判定 + collect.py 存档
                                    (单源 ≤2, 当日默认不限); 旧取数器 fetch_pages.py
                                    保留为 POLICY_FETCH_ENGINE=legacy 对照开关)
  → scripts/extract_records.py    (每份存档一次 hermes -z 单轮微调用: 抽取+研判字段,
                                    key=value 输出, 429/坏输出只废单份)
  → scripts/normalize.py          (入库) → extract_records --stage risk (研判写回)
  → scripts/report.py (Markdown 日报)  → scripts/push_report.py (推送)
                                    [Excel/Word 不在跑批产出, 按需人工生成:
                                     report.py --excel/--docx 或面板一键生成;
                                     推送有 Word 才带附件, 无则正文如实标注]
```

`--agent` 参数回退旧 Agent 会话路径（分块取数 + 会话加工，仅排障/对照用）。
global-policy-search skill 仍为交互式检索入口（会话补数走 `run_scan.sh policy --stage fetch`）。

- 检索词库/源清单/国家优先级全部在 `config/sources.yaml`。
- 政策影响写回用 `scripts/risk.py`(支持 `--list-pending` 列出待研判记录)。

## 取数内核(2026-10-29 起: urlwatch 移植版)

确定性取数段的内核已换成 urlwatch 2.29 引擎的移植版(`scripts/fetchlib/`), 领域层在
`scripts/policy_fetch.py`。**下游一行未改** —— 落盘契约与配额口径不变。

- **两层结构**: `scripts/fetchlib/` 是内核(插件元类 / 作业模型 / 过滤器链 / 缓存抽象 /
  状态机 / 四态判定, 零第三方依赖); `scripts/policy_fetch.py` 是领域层(源清单渲染 /
  链接启发式 / 配额 / 落盘 / 状态汇总)。
- **条件请求**: ETag / If-Modified-Since / 304 —— 上一轮抓过且未改的 URL 不再下载正文。
- **关注面判定**: 判定依据是 `html2text → strip → sha1sum` 的指纹, 不是整页文本 ——
  政府站点的导航栏/日期戳/页脚每轮都在变, 整页比对会把 47 个源全判成 changed 涌进
  LLM 抽取(2026-10-24 token 爆量事故的形态)。
  **指纹防截断(2026-10-30)**: stdlib html.parser 不做隐式闭标签, 页面里 `<form>`/`<svg>`
  等噪声标签没写闭合时其后正文会被当噪声吞掉(变更静默漏检)—— `html_to_text` 检测到
  未闭合噪声标签就把该类标签降级成普通标签重抽; `<script>`/`<style>` 未终结时 html.parser
  会把其后内容整个静默缓冲不交付, 降级救不回来, 检测 rawdata 残留直接正则兜底。
  方向是「宁可指纹多收正文, 不许吞正文」—— 降级只让指纹更敏感, 不会漏检。
- **快照事务(2026-10-30)**: 一个源的所有判定快照(入口页+候选文章页)**延迟提交** ——
  候选处理全程跑完才落库; 半途失败则一个都不提交, 下一轮入口页按旧凭据拿回 200 全文
  重新判「已变更」, 本源候选自动重驱。**两种 collect 失败形态都算「半途失败」**:
  ①`archive_body` 抛异常(60s 超时)→ 异常外抛, 事务自然作废; ②collect 子进程
  **rc≠0**(collect.py 自己崩了, 退出码 1)→ **事务作废 + 本源记失败**(进
  `fetch-status.json` 的 `sources_dead`, 不许伪装成 unchanged/安静日); 输出含
  `[拒绝]` 的当日触顶是全局预算条件、不是本源失败, 照常提交。
  若判定即落库(旧行为), 瞬时失败后下一轮 304/未变 → 整源被跳过, 候选直到列表页再变都
  不会被补抓 —— 瞬时失败被放大成永久丢候选。代价: 事务作废后下一轮重驱时, 本轮已存成
  的候选会再存一份(collect 不按 URL 去重, 只占当日配额; 抽取段按 URL 去重, 不重复烧
  LLM 调用)。
- **两级作业**: 外层一个作业 = 一个源(入口页); 内层按 URL 展开(条件请求的最小粒度)。
  抓取状态以 **URL** 为主键存 `database/fetch.db`(`EPIDEMIC_FETCH_DB` 指路), 不碰
  `epidemic.db`。
- **死源治理(2026-10-30)**: 47 源里 17 个探活即死, 且**死因当场被丢弃**(只留
  `(state=='ok')` 布尔, HTTP 码与降级 note 全丢)→ 事后完全无从分诊。三件事同时修:
  - **降级链补强**(`probe_channels.http_get_full`): 触发条件从字符串前缀
    `'curl-exit-6'` 改为按 **curl 退出码分类**(`RC_RETRYABLE`/`RC_HARD_DEAD`),
    补 `-4`(治 IPv6 半死: 收到响应头但传输卡死)与浏览器 UA 层; 死时把最后
    退出码写进 note —— 死因分诊就靠它。
  - **多入口**(`config/sources.yaml` 的 `alt_urls`): 探活逐个试, 任一入口活即算
    活, 并把**活的那个入口**带回给取数段(取数必须用同一个入口, 否则等于没救)。
    实测救回: co-minagricultura(主入口404/站点根200)、rs-minpolj(http→https)、
    az-afsa、de-fli、in-dahd、cn-sps-aggregator(省级 TBT 站)、ru-rshn-khv。
  - **死因留痕**: `fetch-status.json` 的 per_source 增 `probe_code`/`probe_note`/
    `entry_url`, 并单列 `sources_dead_detail`(含 `cause` A/B/C/D 分类); search-log
    的死源行带上码与 note。分诊脚本 `scripts/dead_sources.py`(逐源 × 逐日表)。
  - 生产实测: **存活 30 → 38 / 47**, 死源 17 → 9, 探活耗时 48.8s。
- **新增交付物**: `data/raw/<日期>/fetch-status.json` —— per-source 状态汇总
  (`sources_alive` / `infra_failure`), 是 `run_scan.sh` 产物终检的输入之一。
  **存活源 0 = 基础设施故障, 不是安静日**, 此时中止本轮、不推送「今日无新增」(H20)。
  **判定看的是「本轮取数段的成败」, 不是状态文件存不存在**(2026-10-29 审查 P0/P2 修):
  取数器崩在写状态文件之前时没有文件可读, 闸门若只看文件存在就会放行 → 空输入短接把
  「取数失败」当成「今日没有新政策」推出 0 条日报。故取数段非零退出 + 0 存档 = 取数失败,
  一律中止不推送(取数失败但已存到东西的不在此列: 有存档就继续加工, 保留断点);
  取数段开始前先清掉上一轮遗留的 `fetch-status.json`, 陈旧文件不参与判断。
- **回退开关**: `POLICY_FETCH_ENGINE=legacy` 切回旧取数器(`fetch_pages.py`)取基线/排障;
  逐日对照候选集合与存档条数, 连续 3 日无差异后删旧分支。
- **逃生开关**: `policy_fetch.py --no-conditional` 忽略条件请求强制重取(防服务器错误 304 漏检)。
- **不引入**: `ShellJob` / `shellpipe` / `diff_tool` 三处任意命令执行面**一律不移植**
  (urlwatch 自带的 shell 作业依赖 POSIX 权限模型, 在本仓库不成立; 且 H6 正在治理注入面)。
- 改内核前先读 `scripts/fetchlib/` 各模块头的来源标注(移植自 urlwatch 的哪一段)。

## 数据合同（改动需同步多处）

权威实现是 `scripts/normalize.py`；人类可读版是 `docs/event-schema.md`。改字段/枚举时必须同步：normalize.py、event-schema.md、两个 prompt、report.py/report_docx.py 展示层。

- **event_id**：sha1(policy|country_en|policy_domain|action_type|主体|生效日期) 前 12 位，幂等主键。重复入库默认按 event_id **合并更新**：空值和兜底默认值（`DEFAULT_SENTINELS`）不覆盖已有核验/研判结论；整行替换须显式 `--replace`。
- **旧闻不入库**：新增记录的发布时间（`source.publish_date`，缺省 `event_date`）距今天超过 `--max-age-days`（默认 7，`POLICY_MAX_AGE_DAYS` 覆盖，0 关闭）即拒收——日报"今日新增"按 first_seen 展示，旧公告一旦入库就会被当成当日新政策；只拦新增 event_id，合并更新不受影响，重放镜像 JSON 重建库时传 0。
- **业务时区固定东八区**（2026-10 新增）：生产 VPS 系统时区是 `Etc/UTC`，而 cron 按 `Asia/Shanghai` 触发。各脚本**不得**直接用 `datetime.date.today()`——北京时间早 07:30 跑批时 UTC 仍是前一天，日报文件名/旧闻窗口/first_seen 会整体错一天（实测 10-15 扫描产出 `2026-10-14-*` 报告）。统一用 `normalize.biz_today()` / `biz_now()`（`report.py` / `push_report.py` / `report_docx.py` / `collect.py` 的 `--date` 默认值都接它）；`run_scan.sh` 另显式 `export TZ` 并把 `$SCAN_DATE` 注入 prompt 与 `report.py --date`，不依赖 Agent 临场决定日期。
- **影响加权合同**（risk.py 强校验，须与 prompts/policy-impact.md 一致）：
  `impact_score = trade*0.35 + biosecurity*0.30 + response*0.25 + alignment*0.10`；≥3.5 高影响 / ≥2.0 中影响 / 其余低影响；高→立即关注、中→持续观察、低→常规记录。四维齐全时 score/level/focus 由代码推导，不得手写冲突值。
- **枚举值是中文**：action_type 收紧|放松|调整|恢复；policy_domain animal|plant|both|trade|measures；impact_type 约束|机会|中性；impact_level 高影响|中影响|低影响；china_relevance 直接涉及中国|间接影响|暂无明显关联；verification_status verified|single_source|unverified|false_positive|merged。
- **可溯源硬要求**：`source.url` 与 `source.quote`（≤120 字）必填；原文没有的信息填 null，严禁编造。**`source.tier=1` 只能由 `normalize.py` 的 `OFFICIAL_DOMAIN_SUFFIXES`（官方域）支撑**——第三方站点（行业媒体、他国贸易门户转述、商业检疫数据库）强制降 tier 3、**且整条拒收不入库**（只可写进 `search-log.md` 当线索），不得判 verified；该国官方域确未收录时在 `source` 里显式加 `official: 1`，此时入库端会自动写 `source.official_note` 留痕待人工复核。新增官方域时同步补进该列表。
- **同一政策跨轮次去重(2026-10-29, M19 根治)**：`event_id` 的 subject 段来自 `policy_key`/`disease_name_en`/`products`/`target_countries`/`scope`——全是**模型自由书写的文本**，同一份公告抽两次、措辞一变就换一个 id，台账里于是出现多条同内容记录（生产实测 283 条里 26 条同政策重复）。现改为：**入库前按语义键查重**，命中就复用已有 `event_id` 合并更新，不新开记录。语义键 = `source.url` + 生效日 + `policy_domain` + `action_type`（见 `normalize.dedup_key`）。**键里必须带领域与动作**：只用「url + 生效日」会并掉 7 条**真政策**（同一页同日既有收紧也有放松，如 gov.uk 动植检页；领域不同的通报也会被并）——2026-10-29 生产实测：带齐四要素可并 26 条、丢真政策 0 条。缺 url 或缺日期则不查重（宁可漏合不错合）。存量重复用 `scripts/merge_duplicates.py` 归并（默认 `--dry-run`，`--apply` 前自动备份），选主规则：已核验 > 已研判 > 正文更全 > `first_seen` 最早。**`event_id` 公式本身不改**（改了要重算全部存量 id，牵动历史引用不可追溯）。
  - **查重取候选走索引列 `source_url`**（`events` 表冗余列，`idx_events_source_url`）：`source.url` 原本只存在于 payload JSON 里，无法建索引，全表解 JSON 在台账上万条后不可接受。**加列 ≠ 生效**——`open_db()` 里必须带**存量回填**（只补 NULL 行，幂等），否则加列之前入库的记录在查重时形同隐身（生产实测：加列后 257 条里只有 19 条有值，其余 238 条永远查不到重）。回填用纯 Python 不用 `json_extract`——`open_db()` 是所有脚本（含只读的日报/面板）的必经之路，不能让它因缺 SQLite json1 扩展而整体报错。剩下三要素（生效日/领域/动作）在 **Python 侧**比对 `dedup_key` 而非塞进 SQL：payload 里的日期可能是 ISO 带时间（`2026-10-29T00:00:00`），与 `events.event_date` 列写法对不上，用列比会漏判重复。
  - **主体冲突闸门（2026-10-29 深夜补）**：语义键分不开**同一页面承载的多条措施**（ePing 一份通报含两条 / gov.uk "topical issues" 滚动页多条且同日同域同动作）。生产实测 19 个同键组里 **3 组是误并**（乌拉圭禁阿根廷禽类 vs 禁西班牙猪；英国禁纳米比亚鲜牛肉 vs 以色列牛胚胎；英国阿根廷禽类解限 vs 南非马匹）。`normalize.subject_conflict()` 以**病害名不相交**判为不同措施、不合并——该规则对 19 组逐条人工判读 **19/19 全对**。**只用病害名，不用目标国**：目标国中英写法不一致（蒙古/mongolia、巴西/brazil）会误伤真重复，且部分记录把「目的市场清单」填在目标国里，语义本身不稳。比较时须剔除泛词（`disease`/`virus` 等），否则 `foot-and-mouth disease` 与 `lumpy skin disease` 会因都含 `disease` 被当成同一病害。
  - **归并方向：更丰富者胜（2026-10-29 深夜修）**：`merge_duplicate()` **不得**让落选记录直接覆盖主记录的每个非空字段——那会把已核验的 SENASA 公告降级成 `Argentina.gob.ar` + `products='bovino'`（生产实测 123 处内容被改差，中影响 3.4 分掉到低影响 1.95 分）。现行语义：标量字段取**信息量更大**的一侧（等量取先到者）、数组字段（`products`/`target_countries`）取**并集**、`source` 按键合并、**研判四维整块取用不混搭**（四维齐全时 score/level/focus 由代码推导，混搭会产出互相矛盾的结论）、核验状态取**更强**的一侧（已核验 > 单源 > 误报标记 > 未核验）。
  - **冗余列必须与 payload 同源**：`upsert()` 用同一个 dict 写列与载荷，二者不得分叉。列是下游筛选依据（`risk.py` 用 `verification_status = 'verified'` 查**列**，`report.py`/面板读**载荷**），一旦不一致就会出现「系统当已核验、报告写待核实」的自相矛盾。历史分叉用一次性脚本修（方向：载荷 ← 列，前提是列上的 verified 来自官方域来源且是**更新的**人工核验动作）。

## 常用命令

无测试套件、无 linter 配置。依赖仅 openpyxl / python-docx / flask，全部**优雅降级**（缺 openpyxl 报告降级 CSV，缺 python-docx 跳过 Word，不要为此加硬依赖）。

```bash
pip install -r requirements.txt
python scripts/collect.py --url <url> --title <标题>  # 原文存档到 data/raw/（可溯源链第一环）
python scripts/policy_fetch.py --date YYYY-MM-DD          # urlwatch 内核取数(条件请求+关注面判定)
python scripts/dead_sources.py --days 10                 # 死源分诊表(逐源 × 逐日: 死因/连续死天数/假活)
python scripts/entry_audit.py --current                  # 源入口验收闸门(判级 A/B/C/X; 改 sources.yaml 的 url/alt_urls 前必跑, 只有 A 级准入)
python scripts/policy_fetch.py --selftest                 # 内核离线自检(不联网, 闭环跑通条件请求→四态)
python scripts/policy_fetch.py --no-conditional --date X   # 忽略条件请求强制重取(防错误 304 漏检)
python scripts/test_fetchlib.py                           # 内核离线测试(42 例, stdlib unittest)
POLICY_FETCH_ENGINE=legacy scripts/run_scan.sh policy    # 临时回退旧取数器(取基线/排障)
python scripts/normalize.py --input <json> --db     # 入库（支持 - 读 stdin）
python scripts/merge_duplicates.py --db <库路径> --dry-run   # 存量同政策重复预览(默认不写库)
python scripts/merge_duplicates.py --db <库路径> --apply     # 执行归并(--apply 前自动备份)
python scripts/risk.py --event-id <id> --dim trade=4 --dim biosecurity=3,...   # 影响写回
python scripts/report.py --date YYYY-MM-DD                # Markdown 日报（跑批只产这个）
python scripts/report.py --date YYYY-MM-DD --excel --docx   # 按需生成 Excel/Word（--policy 已废弃为 no-op）
python scripts/report.py --date YYYY-MM-DD --ensure-docx   # Word 日报缺失才补生成（幂等；人工按需入口——2026-10-25 起跑批/推送不再自动调）
python scripts/push_report.py --date YYYY-MM-DD --dry-run   # 推送预览（企业微信/钉钉/SMTP）
python scripts/push_report.py --date YYYY-MM-DD --review-channel weixin  # 只发微信会话（阶段 1，渠道会话必用；经 hermes send 投递）
python scripts/push_report.py --date YYYY-MM-DD --review-approved  # 审核通过，发完剩余渠道（阶段 2）
python scripts/push_report.py --date YYYY-MM-DD --broadcast    # 显式全量（仅定时任务，且需先有全量范围声明）
python scripts/push_scope.py --date YYYY-MM-DD --review-channel weixin  # 声明本轮只发某渠道（写范围锁）
python scripts/push_scope.py --date YYYY-MM-DD --broadcast     # 声明本轮全量（定时任务路径）
python scripts/push_report.py --date YYYY-MM-DD --force      # 当日已推送过，强制重发
python scripts/diagnose_push.py                              # 推送链路现场诊断（只读）
python webapp/app.py                                        # Flask 面板，默认 0.0.0.0:8000
scripts/run_scan.sh policy                                  # 定时扫描入口（am|pm|policy）
scripts/run_scan.sh policy --review-channel wechat          # 渠道会话触发的扫描：只发该渠道
scripts/run_scan.sh policy --broadcast                      # 定时任务扫描：全量（写全量范围锁）
scripts/run_scan.sh policy --stage fetch                    # 只跑取数段（会话/人工补数入口；写 .fetch-done 幂等标记）
scripts/run_scan.sh policy --refetch                        # 强制重取（清当日 .fetch-done 标记，显式留痕）
scripts/run_scan.sh policy --stage fetch --only-group 4     # 小token测试：只跑第 4 块（每块约 6 源对；不写标记、不挡正式取数）
scripts/run_scan.sh policy --agent                          # 回退旧 Agent 会话路径（默认为 P4 确定性管线）
python scripts/token_budget.py --date YYYY-MM-DD            # 当日 Hermes token 用量/预算检查（只读 state.db）
bash scripts/deploy_update.sh policy                        # 生产更新（git pull + 依赖 + 自检）
```

## 模块边界与运行方式

- scripts 内**同级导入**：risk.py `from normalize import ...`；report.py 和 webapp/app.py 靠 `sys.path.insert` 引入 scripts/ 与 templates/（Excel 样式工具在 `templates/base.py`）。因此一律从**仓库根目录**以 `python scripts/xxx.py` 运行；`run_scan.sh` 特意 `cd "$REPO"` 即为此（勿删）。
- 数据落地：`data/raw|events|reports/` 与 `database/`、`logs/` 均不入库（gitignore）。生产 VPS 用 `EPIDEMIC_DATA_DIR` / `EPIDEMIC_DB` 指到 repo 之外（/opt/global-epidemic-ai）。
- 环境变量：run_scan.sh 自动加载 `~/.hermes/.env`；推送渠道 `WECHAT_WEBHOOK` / `DINGTALK_WEBHOOK`(+`DINGTALK_SECRET` 加签) / `SMTP_*`；Hermes 投递型渠道由 `hermes send` 自行读 `~/.hermes/.env` 的 `WEIXIN_*` / `FEISHU_*`，可用 `HERMES_PUSH_CHANNELS=weixin,feishu` 把它们纳入定时全量、`HERMES_CLI` 指定 hermes 命令路径；面板 `DASHBOARD_HOST/PORT`、`RADAR_GEO`、`RADAR_POLICY_GENERATE_CMD`、`RADAR_PUBLIC_URL`；Hermes 无头调用模板 `HERMES_RUN_CMD`（`{PROMPT}` 占位符，未配置时退化为待执行提醒推送）。
- **评审核闸门(2026-10-19 引入, 当晚加固)**：用户在某渠道（微信/钉钉/邮箱）提出测试类请求时，强调"反馈必须先在原渠道完成再扩散到其它渠道"。`scripts/push_report.py` 用 `--review-channel <渠道>`（等价 `REVIEW_CHANNEL` 环境变量）启用两段式：
  - **阶段 1**：`push_report.py --date X --review-channel weixin`（用户与 Hermes 说话的个人微信会话）→ 只发审阅渠道，写 `data/reports/.review-pending/<日期>.flag`（首行 `review_channel=<显示名>` 供阶段 2 读回），不写 `.pushed/`；其它渠道不推送。
  - **阶段 2**：用户在审阅端确认后再调 `push_report.py --date X --review-approved` → 把审阅渠道之外的剩余渠道发完，写 `.pushed/`，清理 `.review-pending/`。**审阅渠道以 `.review-pending` 标记里记录的渠道为准**（`--review-channel` 只作兜底）：阶段 2 换会话/换机器执行时容易忘带参数，原实现会静默退化成"全量推送"，让审阅渠道重复收到日报、直接违反"先在原渠道完成再扩散"的红线——2026-10-19 已修。
  - **默认不扩散**：不带 `--review-channel` / `--review-approved` / `--broadcast` 三个之一时，**脚本不推送并以退出码 2 提示**。全量推送必须显式 `--broadcast`（仅定时任务用）。这是刻意的 fail-closed：忘了指定的后果是"没发"（用户马上发现），而不是"发到所有渠道"（要等别人投诉）。
  - **fail-closed 取值校验**：`--review-channel` / `REVIEW_CHANNEL` 认不出的值（如 `weixn`）**报错退出**，不再静默丢弃——原实现丢弃后等价于"没设闸门"→全量推送，中文"企业微信"、简写"wx" 这类写法最常踩。中文别名（企业微信/钉钉/邮箱/微信/飞书）与简写（wx/wecom/email/weixin/lark）现已纳入白名单。
  - **两类渠道（2026-10-20 生产现场新增）**：①**webhook 型**（脚本自己 POST）——企业微信群机器人 `WECHAT_WEBHOOK` / 钉钉 `DINGTALK_WEBHOOK` / 邮箱 `SMTP_*`；②**Hermes 投递型**（借 `hermes send` 投到 Hermes 已配置平台）——**微信 `weixin`**（个人微信会话）/ 飞书 `feishu`。生产现场查证：`.env` 里**没有** `WECHAT_WEBHOOK`，用户说的"微信会话"是 Hermes 的个人微信通道（`WEIXIN_*`），脚本自己发不进去，只有 `hermes send --to weixin` 能把日报送进那个会话（支持 `MEDIA:<path>` 顺带发 Word 报告）。**"微信"→weixin、"企业微信"→wechat，别混**。
  - **Hermes 渠道默认不参与定时全量**：`--broadcast` 的推送集合 = 已配 webhook 渠道（钉钉）+ `HERMES_PUSH_CHANNELS` 指定的 Hermes 渠道（默认空）——免得日报突然开始刷屏用户微信。要把日报也送到微信，用 `HERMES_PUSH_CHANNELS=weixin` 显式打开。
  - **待审期间不得扩散**：存在 `.review-pending` 标记时，任何推送（含 `--force` 与 `--message`）都只发审阅渠道，`--broadcast` 会被挡下。
  - `--message` 自定义文本在**没有**待审标记、也**没有**指定审阅渠道时照常群发（cron fallback 的"待执行提醒"不能被卡）。
  - Hermes 在某渠道会话中复述"先在原渠道完成反馈"的口径时，应在该渠道对应的 `push_report.py` 调用中显式传 `--review-channel <该渠道>`；用户在原渠道确认后再以 `--review-approved` 释放剩余渠道。**不要用不带渠道参数的全量命令**。
- **推送范围锁(2026-10-20 钉钉误推事故后引入, 决定性)**：闸门此前是"提示式"的——「本轮是渠道会话还是定时任务」交给 Agent 临场判断，而 `run_scan.sh` 是 cron 与手动补跑**共用**的入口，手动补跑在 prompt 眼里就是"定时任务那条路"，于是 Agent 选了 `--broadcast`，日报发到了钉钉；且 `run_scan.sh` 里**根本没有 REVIEW_CHANNEL 传递机制**。现在推送范围**由发起方声明并落盘成锁**：
  - **锁文件**：`data/reports/.push-scope/<日期>.lock`（`scope=review|channel=…` 或 `scope=broadcast`），由 `scripts/push_scope.py` 写、由 `push_report.py` 读**执行**。作者：cron 路径 = `run_scan.sh <模式> --broadcast`；渠道会话 = `push_report.py --review-channel <渠道>`（阶段 1 自己会写锁）。
  - **`--broadcast` 不再自声明**：没有 broadcast 锁 → 拒绝（退出码 1，提示走 `run_scan.sh --broadcast` 或 `push_scope.py --broadcast --source <依据>`）；锁是 review → 拒绝（"本轮只发 X"）；只有调度路径声明过的 broadcast 锁才放行。**全量声明还必须写明依据**：`push_scope.py --broadcast` 的 `--source` 留空即拒绝（10-20 现场那条 `source=manual` 的锁就是一次没写依据的全量声明）。
  - **run_scan.sh 不再让 Agent 二选一**：`--review-channel <渠道>` / `--broadcast` 显式参数写进 prompt（确定性指令，无"若是…若是…"），并 `export REVIEW_CHANNEL` 给下游；两者都不给时，**cron 上下文**（祖先进程含 cron/crond）自动按定时任务全量处理，**其它上下文**（手动/Agent 触发）本轮**不扩散**（`--broadcast` 一律拒绝）。**显式 `--broadcast` 也必须在 cron 上下文里才成立**（非 cron 拒绝并退出码 2）——手动补跑与 cron 共用入口，允许它自称定时任务就是把"谁发起的"又交回给调用方。人工运维确需全量补发走 `push_scope.py --broadcast --source manual:运维补发` + `push_report.py --broadcast`（显式留痕）。
  - **阶段 2 无待审记录即拒绝**（原实现是"警告一下照常全量发"，又一个"忘了→发到所有渠道"的口子）。
  - **广播锁有有效期 + 失败即撤销声明**（2026-10-22 检索失败事故后加，红线级）：cron 在跑批**开始**时就写下 broadcast 锁，但那一轮可能根本没跑完（10-22 实测：检索卡死 44 分钟、Agent 放弃、rc=0 假成功，推送从未发生）。锁若无限期留着，当天任何时候 `push_report.py --broadcast` 都会被它放行 → 群发钉钉，**正是 10-20 事故的重演路径**，且 cron 上下文校验拦不住（锁是 cron 自己写的，来源看着就合法）。现在两道堵：①锁的有效期 `SCOPE_MAX_AGE_MINUTES=75`（跑批硬上限——取数拆组后最坏 ~70 分钟 + 推送余量；2026-10-24 由 60 调至 75），过期即拒绝放行并要求重新声明（人工运维须写明 `--source`）；②`run_scan.sh` 在那一轮没推成时**当场 `push_scope.py --clear`** 撤销声明。
  - **跑批"假成功"不得再发生**（2026-10-22 加固）：Agent 放弃时退出码是 0，`run_scan.sh` 直接透传会让 cron 记成功 → 故障静默一整天。现在 `run_scan.sh` 跑完**校验产物**（日报 md + 按范围锁决定的推送/待审标记，且必须是"这一轮新生成"的，按 mtime 判定），校验不过 → `[失败]` + 退出码 1；校验通过才补 Word。另有 `timeout` 硬上限（默认 2400s，防白烧 40 多分钟）与网关健康预检（`hermes status`，最多等 180s；10-20 实测撞上网关重启 `Gateway draining` 时整轮扫描 8 秒就废了）。
  - **取数段收尾产物兜底（2026-10-22 晚）**：拆段修复后仍有 3 轮实测失败——取数段 Agent 每轮都在"存档完成、写 `candidates.json` 之前"耗尽单轮工具调用预算（当天 6 条候选原文全部落盘，候选清单却始终没写出来），加工段按"文件不存在就跳过"指令空转出报，**6 条原文一条没进库**。两道修复：①取数段 prompt 把 `candidates.json` / `search-log.md` 从"收尾交付"改为"**每存档一条立即追加**"；②`run_scan.sh` 在取数段结束后（无论成败，`--stage process` 补跑同样）调用 `scripts/synth_candidates.py` 从当日 meta.json **无损合成**最小版候选清单（title/url/域名，`country_en`/`publish_date` 等语义字段留空，由加工段照抽取规则从存档原文补齐），脚本幂等、不联网、不编造、与已有 candidates.json 按 url 去重合并（Agent 已写的字段不会被覆盖）。
  - **加工段冷启动超时回归（2026-10-22 深夜）**：拆段后当晚三轮实测全部撞死在加工段 900s 超时——而拆段前旧单轮流程 10-11~10-21 每轮全程仅 3-7 分钟。根因：拆段把抽取留给了加工段，而加工段是**全新冷会话**，必须把全部存档原文读进上下文（12 条 ≈ 12 万 token）再逐条抽取，旧流程"搜到一条、趁原文在手当场抽取"的零成本优势被丢掉了。修复：**取数段就地抽取**——每存档一条，立即把该条的 policy 记录追加写进 `extracted-policy.json`（与 candidates.json 同一条边存边写纪律）；**加工段降级为轻收尾**——主输入改为 `extracted-policy.json`，对照 candidates.json 只补「已存档但未抽取」的漏条（先查 search-log.md 是否已判不合格，确实漏抽才读原文），严禁重读已抽取条目的原文，随后 normalize → risk → report → push。synth_candidates.py 兜底仍只合成 candidates.json（抽取需语义理解，脚本无法无损合成；取数段崩溃导致 extracted-policy.json 缺失时由加工段补抽兜底）。另两处同日修复：`run_scan.sh` 的 `timeout` 只能杀 CLI 等待进程、杀不死网关侧 Agent（超时判定失败后 Agent 仍在跑、仍在耗配额，13:01 一轮"超时后反而 rc=0 成功"即孤儿 Agent 侥幸写出日报），2026-10-23 已修（见下条）；Ubuntu cron 不认 crontab 里的 `TZ=` 调度（只当作业环境变量），当天误改成 `30 7` 后实际按 UTC 触发＝北京时间 15:30 跑批，已改回 `30 23`（UTC）＝北京 07:30。
  - **取数段 token 经济三连修（2026-10-23 引入）**：10-23 实测取数段 90 轮/22 分钟/输入 12.9M token（上下文 21k→233k 逐轮全量重发），与加工段合计 30 分钟灌 ~16M 进 MiniMax 窗口触发 429。三个病根与对应修复：①**约 20 个死通道每天重新探测**——新增 `scripts/channel_health.py`（确定性、只读、不联网），从近几日 search-log.md 提取不可用通道，`run_scan.sh` 在取数段启动前把名单注入 prompt（连续两日死→本轮直接跳过；单日死→至多一次 5 秒复检；通道名跨日写法有出入，匹配前做归一）；②**一通道一次调用 + 整页正文裸进上下文**——取数 prompt 改"批次化探测"（同组通道一个 bash for 循环一次探完，只回显状态清单）+"先落盘再摘录"（判断内容只准 grep/python 摘 ≤2KB，单次工具输出 >30KB 判违规）；③**就地抽取改从存档文件抽**（grep/python 摘 quote/日期，每次 ≤2KB、逐字摘自存档文件），并新增红线：取数段严禁运行 normalize/risk/report/push（防抢跑把 first_seen 记错日子——10-23 凌晨实收：收尾孤儿 Agent 在北京零点后入库，把前日存档记成当日 first_seen）。预期省 50-70% 取数段 token。**另(同日代码复查发现, 红线级)**: run_scan.sh 的 prompt 是双引号赋值、最终经 `eval` 拼进 `hermes -z "..."` —— prompt 文本与任何注入内容里**严禁出现 ASCII 双引号/反引号/$**：双引号会把赋值从中间切断, `bash -n` 不报错但运行时 `PROMPT_FETCH` 为空、整轮静默退化走错分支(cron 场景=空日报照常推送)。纪律: prompt 内示例命令一律用单引号(如 `curl -w '%{http_code}'`), 中文强调用「」; 通道体检名单在 channel_health.py 输出与 run_scan.sh 注入处做两道清洗。改动 prompt 后必须跑运行时干跑验证(桩掉 HERMES_RUN_CMD 落盘 prompt 再断言), `bash -n` 不够。
  - **孤儿 turn 终止（2026-10-23 引入）**：受控实验确认 `hermes -z` 只是把 prompt 经 websocket 提交给网关，turn 由网关守护进程执行——`timeout` 杀掉的 CLI 只是断开 ws（agent.log 可见 `client_disconnect`），网关侧照跑到 max_turns（10-22 实测孤儿跑满 90 轮 ≈ 8M token；10-23 取数段孤儿超时后仍多跑并抢跑 normalize）。网关没有按会话终止的 CLI（`pause` 只挡新 turn、`sessions` 无 stop、控制套接字无 interrupt 动词、`--resume` 新 turn 只会在 FIFO 里排队），且网关看门狗是**不活动**触发——活跃的孤儿永远打不断。唯一确定性手段：阶段超时（rc 124/137）后 `hermes gateway restart`——`restart_drain_timeout=0` 立即硬中断所有在跑 turn，被中断会话按 Hermes 语义"下条消息续跑"，用户微信在途消息只是晚点回。run_scan.sh 的 `_abort_gateway_orphans` 在取数/加工/单轮三处超时分支调用，重启后等待网关就绪再进下一段；`SCAN_ORPHAN_RESTART=0` 可关闭。
  - **取数幂等锁 + 日预算刹车(2026-10-24 token 爆量事故后引入, 红线级)**：当天 6 个扫描会话烧穿 Token Plan（429，连微信对话都调不动），而 6 条存档 09:39 已落齐——12:50/13:54/14:55 三轮重复取数零新增、白烧 ~126 万输入 token + 3200 万 cache 读（其中 4 个会话绕过 run_scan.sh 直接 `hermes -z`：无超时看护无幂等检查，12:50 那轮还在用部署前的旧 prompt 重读 58KB sources.yaml、12 个文件 125KB）。三道修复：①**取数幂等锁** `data/raw/<日期>/.fetch-done`——取数段跑过且交付（rc=0 或已有存档）即写标记，之后任何取数请求（含手动）看到标记直接跳过进加工，强制重取必须显式 `--refetch`（超时且 0 存档不写标记，允许下一轮重取）；②**日预算刹车** `scripts/token_budget.py`（只读 `~/.hermes/state.db`，可用 `HERMES_STATE_DB` 改路径，fail-open）——东八区当日 Hermes 折合消耗（input+output+cache_read×0.1，`SCAN_CACHE_WEIGHT` 覆盖）≥ 预算（默认 400 万，`SCAN_TOKEN_DAILY_BUDGET` 覆盖）时取数段不再启动，降级为只加工已有存档（数据不丢，损失止于当日增量），并在 search-log.md 留痕；③**统一发射口** `--stage fetch`——会话/人工补数一律走 run_scan.sh（自带超时看护/幂等锁/预算刹车，scope 强制 none 不碰推送），**严禁自行拼 hermes -z 扫描 prompt 或复刻取数指令**（global-policy-search skill 红线已同步）。
  - **取数分块 + 通道确定性预探(2026-10-24 P1, 10-25 晨跑再修)**：取数单作业不收敛——10-24 两轮 90/90、两轮 50/50 打满；10-25 必扫 21 源对单组仍 50/50 打满且 Agent 把候选**囤在对话里 0 交付**——①**按 6 源对/块切块**：`scan_targets.py` 把当日全部源对（必扫+轮转，按固定优先级序）按 `GROUP_CHUNK=6` 切成 K 块，`--group N` 渲染第 N 块、`--groups` 输出变长 `n1=.. nK=..`；run_scan.sh 逐块发起、超时按源对数加权（保底 360s）、单块打满只废单块（存档边存边写不受牵连），prompt 模板 `@@COUNTRY_BLOCK@@` 逐块替换、纪律含「进度自检：扫到第 3 个通道时 data/raw 下必须已有存档」；解析失败退回单组全量旧口径；②**通道 HTTP 探测前移出 Agent**：新增 `scripts/probe_channels.py`（curl 8 并发、单通道 6s、只看状态码，[dead]=非 2xx/3xx 或连接失败，与 channel_health「不可用」同口径），状态清单注入 prompt，Agent 对 [dead] 只记 search-log 一行不再花工具调用——治理后一轮仍有 26 terminal + 25 execute_code 的探测/摘录调用，这是砍掉的第一块；③`SCOPE_MAX_AGE_MINUTES` 60→75（分块后最坏 ~70 分钟，防止"这一轮还在跑"的合法广播锁被误判过期）。
  - **空输入短接(2026-10-25 晨跑事故后引入, P4 前后共用)**：取数 0 交付（0 存档且无候选/抽取记录）时，加工段**不派 Agent**——run_scan.sh 直接 `report.py`（只出 Markdown，Excel/Word 按需）确定性出报、按范围锁确定性推送（broadcast/review 才推）、自动补一份说明性 policy-findings.md。实测 10-25：空清单下加工 Agent 无视「不联网检索」红线自发联网重做取数（194 万折合、50 轮打满、入库/出报一步没跑到）。
  - **P4 确定性管线(2026-10-25 定稿, 10 分钟硬约束驱动)**：Agent 会话对「轮次上限 / Token Plan 耗尽(429) / 阶段超时」任一死法都是**整轮全损**(10-25 三连败实证: 50/50 打满 0 落盘、空输入抢跑、429 切断), 而用户约束全链路 ≤10 分钟。方案=脚本开车、LLM 只当顾问: ①`scripts/fetch_pages.py` 确定性取数(probe_channels 预探复用跳死通道 → curl 8 并发抓入口页 → 链接抽取 + 多语种日期窗口过滤(7 天) → collect.py 存档(单源 ≤2 条; 当日总条数 2026-10-28 起默认不限——取数零 LLM 后 12 条/日反成覆盖瓶颈, 10-28 实测配额被栏目页耗尽致 12 条全拒) → candidates.jsonl 逐条追加 → search-log 逐源留痕), **零 LLM**; ②`scripts/extract_records.py` 每份存档**一次** hermes -z 单轮微调用(prompt=字段模板+剥 head 后正文摘录≤8KB——2026-10-28 起读**全文**定位正文, 旧版只读文件头 8KB 让大 head 页面全军覆没(12 条全被「无正文」拒收, 阿根廷新城疫真政策被误杀), 输出 key=value 行, 本地解析嵌套 source.* 并校验 url/quote), 研判四维并入同一次调用, `--stage risk` 在 normalize 后按当日 url→event_id 确定性写回; 单份失败(429/超时/坏输出)重试 1 次后跳过留痕, **429 快速失败不烧重试**; ③`run_scan.sh policy` 默认走此管线, `--agent` 回退旧路径; prompt 模板与输出均不含 ASCII 双引号/反引号/$。**诚实取舍**: 链接启发式召回率低于 Agent 浏览(无日期线索只记 search-log 线索; 需 web_search 发现的条目如输华议定书类暂不覆盖), 覆盖率后续按源补 pattern(ePing 优先)。
  - **存档配额口径(2026-10-27 修正, A1)**：当日存档上限一律按**存档条数**计(`*.meta.json` 个数 = 条数), 交付文件(`candidates.jsonl` / `search-log.md` / `extracted-policy.jsonl` / `policy-findings.md` / `.fetch-done`)**不占配额**。原 `collect.py` 数的是目录**文件总数**, 而每条存档产出两个文件(正文 + `.meta.json`)再加交付文件 → 标称"≤12/日"实际只存得下 **6 条**, 而 `fetch_pages` 的 `already` 与 `run_scan.sh` 的 `RAW_N` 终检都按条数计, 两套口径不一致, 47 源的每日覆盖被悄悄压掉一半(隔离实测: 修复前第 7 次即被拒, 修复后第 13 次才拒)。现由 `collect.count_archives()` 统一口径, `fetch_pages` 复用同一函数; 参数改名 `--max-archives-per-day`, 旧名 `--max-files-per-day` 保留为别名(两个都给且不一致 → 报错退出)。
  - **工具输出硬上限(2026-10-24 P2)**：「≤2KB 摘录 / 8KB 单次红线」从 prompt 纪律升级为**脚本保证**——新增 `scripts/jsonl_append.py`（JSONL 单行追加器：只回执一行 `ok +N`、点号键落嵌套 `source.*`、空值存 null、缺标题/URL 拒追加防残行）与 `scripts/snip.py`（摘录器：默认/--grep 都硬性截到 ≤2KB 并带截断标注），取数/加工 prompt 已钉死两者用法，并**内联抽取键值模板**（顺带补上 92dbe1e 的缺口：prompt 禁读仓库文件、却让 Agent「按 prompts/policy-extraction.md 的字段规则」抽——字段规则既不在上下文里又不许读）。网关侧 `~/.hermes/config.yaml` 的 `tool_output.max_bytes` 部署时 50000→12000（全局硬兜底， 与 max_turns 同属 VPS 侧配置， 不在仓库内）。
  - **生产更新**：`bash scripts/deploy_update.sh policy`（git pull + 依赖 + 自检）。仓库只有 clone 没有更新机制，生产不 pull = 闸门加固根本没生效——这正是 10-20 事故的载体。
  - **现场诊断**：`python scripts/diagnose_push.py` 一次打印：运行代码 commit（与远端是否一致）、当日范围锁/待审/已推标记、渠道配置（自动读 `~/.hermes/.env`，只配一个渠道时会提示"先在微信审阅"跑不通）、crontab 是否显式声明范围、调度日志尾部（自动找 `cron.log`/`scan.log`，有无 `scope=` 标记＝新旧版 run_scan.sh）。推送内容尾部也带 `代码 <commit>` 指纹，`.pushed` 标记带**范围与声明来源**。
- **Word 附件（2026-10-20 现场发现 → 2026-10-25 调整为按需）**：10-20 的事故形态是"Word 由 Agent 照 prompt 执行 `--docx`、跑批漏了、推送侧 `if isfile` 静默跳过附件，用户以为有 Word 却只有正文"。当时修法是跑批/推送两道 `--ensure-docx` 自动补齐 + 如实标注。**2026-10-25 起语义反转：跑批只产 Markdown 日报，Excel/Word 按需人工生成**（`report.py --excel/--docx` 或面板一键生成），跑批与推送都不再自动生成/补齐：
  - 推送按"文件在才带附件"执行：Word 存在就随邮件/Hermes 附上；不存在正文页脚写「⚠️ 本次无 Word 附件(Word 按需生成: report.py --docx)」，控制台打 `[提示]`，`send_hermes` / `send_mail` 附件缺失仍打 `[WARN]` 不静默。
  - `report.py --ensure-docx` 保留为**人工按需入口**（幂等；md 缺失不补，防空壳 Word 掩盖跑批失败——10-22 教训）。
  - `report.py --docx` 的异常一律优雅降级但留痕（不崩、不装没看见）。
- **推送卡片内容口径（2026-10-27 用户定）**：IM 卡片每条 = **标题（超链到原文）+ 正文是整个政策的简介**。
  - 正文用抽取阶段写的 `summary_cn` **全文**（缺失退 `summary_en`，再缺则不写正文行——不拿标题冒充简介、不编造）；**不再**拿截断到 60 字的 `impact_rationale` 顶替（研判理由仍留在完整 Markdown/Word 报告里）。
  - 标题保留 `[标题](原文URL)`；无 URL 时不写空链接 `]()`。
  - **所有渠道同一份文本**：微信侧审阅推送的就是钉钉那份正式内容（用户原话「推微信侧的测试内容，需要和正式推送钉钉的要一致，不然就不是测试了」）——不做渠道特供渲染。
  - **条目之间留空行**：微信单条上限 2000 字且按 markdown 块打包，整段超限会被**静默截断**（`gateway/platforms/helpers.py` 的 `greedy_pack_blocks` overflow 分支）；留空行后每个条目自成一个块，超限时按条目边界切成多条，一条政策都不丢。
  - `_cut` 按渠道字节上限截断时必带「已截断」提示，不静默丢内容（企业微信 3800 字节上限的遗留见 `docs/known-issues.md` M18）。
  - 微信投递报错 `iLink sendmessage session not ready` 是**会话未就绪**（需对方先给机器人发消息），与 target 解析无关，**不要**为此加重试或改 `.env` target——见 `docs/known-issues.md` M17。

## 约定与注意事项

- 面向用户的文案（docstring、CLI 输出、校验报错、日报、文档）**一律中文**；代码注释也用中文，风格贴近现有 `%` 格式化、stdlib 优先（urllib 而非 requests）。
- 开发在 Windows（注意 CJK 字体：templates/base.py 按 platform 选 Microsoft YaHei / Noto Sans CJK SC），生产是 Linux VPS。
- webapp 面板只读 + 一键生成日报；events API 需显式 `record_type=policy` 才返回数据。勿裸暴露公网。
- 地理编码走 OSM Nominatim，缓存于 `data/geocache.json`，`RADAR_GEO=0` 关闭；同国多点做确定性偏移。
- cron 部署要求时区 Asia/Shanghai，且需在 crontab 手工补 PATH（见 config/crontab.example）。
- 提交信息用 conventional commits（feat/fix/docs/chore(scope)），详见 git log 风格。
- 敏感区改动前先读：`docs/event-schema.md`、`prompts/policy-impact.md`（研判口径）、`config/sources.yaml`（源分级 tier、track 轨道、schedule 档位与国别周轮转分片）。
- **源的 `track` 决定是否进入 policy 档主动扫描**：`policy`=政策动作源（主动扫）；`outbreak`=疫情事件源（不扫，仅研判背景）；`background`=中国侧与标准名录（不扫，仅背景）。2026-10 起 policy 档只扫 `track: policy`——疫情源在本分支几乎不产出政策动作却吃检索预算。改源清单时新增源必须显式写 `track`。
  - **例外（2026-10-15 确认, 2026-10-18 改）**：`cn-sps-aggregator` 在 2026-10-15 被放宽为 tier 1+`track: policy`；2026-10-18 决定**仅省级 TBT 咨询中心 + 商务部**留作 tier 1（5 个站），**`tbtsps.cn`（海关总署标法中心）撤除**——它同时大量转载中国海关总署公告与商务部贸易环境专刊，混进政策台账会绕过"中国自身措施不入台账"的红线，且其转载内容会被下一轮 cron 当原料回抓，形成信源同质化循环。`tbtsps.cn` 同步移出 `OFFICIAL_DOMAIN_SUFFIXES` 并加入 `THIRD_PARTY_HINTS` 双重保险。其余商业 `.com`/`.org` 聚合站（如 `tbtguide.com`、`c1f.com`）不在此列，仍强制降 tier 3。
- **国别主干（2026-10-25 起每日全扫）**：`policy_watch_countries` 六档**全部每天进清单**——P4 确定性取数（curl，零 LLM）后源数不再消耗 token（抽取成本只跟存档数走，单份极小；当日存档 2026-10-28 起默认不限，防洪水位是单源 ≤2），周轮转分片失去省 token 的存在前提，`shard` 字段与 `weekly_shards` 段已废弃（保留仅为兼容解析器，无过滤效果）。**新增 policy 源必须挂到某个国别的 `source_ids`**，否则不会被主动定位到（2026-10 修正了 16 个孤儿源，含 ru-fsvps/jp-maff/eu-sante-sers 等）。历史教训（H17）：10-24~10-25 期间轮转档因 `weekly_shards` 配置断裂导致 23 个源无任何调度日，每日全扫后此缺陷类别整体消除；自测有"任意两天清单一致 + 六档全在 + 7 天并集=每日清单"不变量防回归。
