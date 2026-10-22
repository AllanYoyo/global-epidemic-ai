# AGENTS.md · 疫见全球（policy 分支）

外国政府/国际组织动植物检疫**政策变化**监测系统。数据合同只允许 `record_type=policy`（疫情事件管线已在本分支移除，勿重新引入）。中国海关总署公告**不作为政策采集源**，仅在影响研判时作背景。Agent 全流程（检索→抽取→核验→研判）由 Hermes 执行；`scripts/` 是确定性工具层。

## 生产链路

```
global-policy-search (skill, 检索入口)
  → prompts/policy-extraction.md (结构化抽取)
  → policy-verification (skill, 官方核验)
  → prompts/policy-impact.md (对华影响研判)
  → scripts/report.py + report_docx.py (日报) → scripts/push_report.py (推送)
```

- 检索词库/源清单/国家优先级全部在 `config/sources.yaml`。
- 政策影响写回用 `scripts/risk.py`（支持 `--list-pending` 列出待研判记录）。

## 数据合同（改动需同步多处）

权威实现是 `scripts/normalize.py`；人类可读版是 `docs/event-schema.md`。改字段/枚举时必须同步：normalize.py、event-schema.md、两个 prompt、report.py/report_docx.py 展示层。

- **event_id**：sha1(policy|country_en|policy_domain|action_type|主体|生效日期) 前 12 位，幂等主键。重复入库默认按 event_id **合并更新**：空值和兜底默认值（`DEFAULT_SENTINELS`）不覆盖已有核验/研判结论；整行替换须显式 `--replace`。
- **旧闻不入库**：新增记录的发布时间（`source.publish_date`，缺省 `event_date`）距今天超过 `--max-age-days`（默认 7，`POLICY_MAX_AGE_DAYS` 覆盖，0 关闭）即拒收——日报"今日新增"按 first_seen 展示，旧公告一旦入库就会被当成当日新政策；只拦新增 event_id，合并更新不受影响，重放镜像 JSON 重建库时传 0。
- **业务时区固定东八区**（2026-10 新增）：生产 VPS 系统时区是 `Etc/UTC`，而 cron 按 `Asia/Shanghai` 触发。各脚本**不得**直接用 `datetime.date.today()`——北京时间早 07:30 跑批时 UTC 仍是前一天，日报文件名/旧闻窗口/first_seen 会整体错一天（实测 10-15 扫描产出 `2026-10-14-*` 报告）。统一用 `normalize.biz_today()` / `biz_now()`（`report.py` / `push_report.py` / `report_docx.py` / `collect.py` 的 `--date` 默认值都接它）；`run_scan.sh` 另显式 `export TZ` 并把 `$SCAN_DATE` 注入 prompt 与 `report.py --date`，不依赖 Agent 临场决定日期。
- **影响加权合同**（risk.py 强校验，须与 prompts/policy-impact.md 一致）：
  `impact_score = trade*0.35 + biosecurity*0.30 + response*0.25 + alignment*0.10`；≥3.5 高影响 / ≥2.0 中影响 / 其余低影响；高→立即关注、中→持续观察、低→常规记录。四维齐全时 score/level/focus 由代码推导，不得手写冲突值。
- **枚举值是中文**：action_type 收紧|放松|调整|恢复；policy_domain animal|plant|both|trade|measures；impact_type 约束|机会|中性；impact_level 高影响|中影响|低影响；china_relevance 直接涉及中国|间接影响|暂无明显关联；verification_status verified|single_source|unverified|false_positive|merged。
- **可溯源硬要求**：`source.url` 与 `source.quote`（≤120 字）必填；原文没有的信息填 null，严禁编造。**`source.tier=1` 只能由 `normalize.py` 的 `OFFICIAL_DOMAIN_SUFFIXES`（官方域）支撑**——第三方站点（行业媒体、他国贸易门户转述、商业检疫数据库）强制降 tier 3、**且整条拒收不入库**（只可写进 `search-log.md` 当线索），不得判 verified；该国官方域确未收录时在 `source` 里显式加 `official: 1`，此时入库端会自动写 `source.official_note` 留痕待人工复核。新增官方域时同步补进该列表。

## 常用命令

无测试套件、无 linter 配置。依赖仅 openpyxl / python-docx / flask，全部**优雅降级**（缺 openpyxl 报告降级 CSV，缺 python-docx 跳过 Word，不要为此加硬依赖）。

```bash
pip install -r requirements.txt
python scripts/collect.py --url <url> --title <标题>  # 原文存档到 data/raw/（可溯源链第一环）
python scripts/normalize.py --input <json> --db     # 入库（支持 - 读 stdin）
python scripts/risk.py --event-id <id> --dim trade=4 --dim biosecurity=3,...   # 影响写回
python scripts/report.py --date YYYY-MM-DD --excel --docx   # 日报（--policy 已废弃为 no-op）
python scripts/report.py --date YYYY-MM-DD --ensure-docx   # Word 日报缺失才补生成（幂等；跑批/推送的兜底入口）
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
  - **广播锁有有效期 + 失败即撤销声明**（2026-10-22 检索失败事故后加，红线级）：cron 在跑批**开始**时就写下 broadcast 锁，但那一轮可能根本没跑完（10-22 实测：检索卡死 44 分钟、Agent 放弃、rc=0 假成功，推送从未发生）。锁若无限期留着，当天任何时候 `push_report.py --broadcast` 都会被它放行 → 群发钉钉，**正是 10-20 事故的重演路径**，且 cron 上下文校验拦不住（锁是 cron 自己写的，来源看着就合法）。现在两道堵：①锁的有效期 `SCOPE_MAX_AGE_MINUTES=60`（跑批硬上限 40 分钟 + 推送余量），过期即拒绝放行并要求重新声明（人工运维须写明 `--source`）；②`run_scan.sh` 在那一轮没推成时**当场 `push_scope.py --clear`** 撤销声明。
  - **跑批"假成功"不得再发生**（2026-10-22 加固）：Agent 放弃时退出码是 0，`run_scan.sh` 直接透传会让 cron 记成功 → 故障静默一整天。现在 `run_scan.sh` 跑完**校验产物**（日报 md + 按范围锁决定的推送/待审标记，且必须是"这一轮新生成"的，按 mtime 判定），校验不过 → `[失败]` + 退出码 1；校验通过才补 Word。另有 `timeout` 硬上限（默认 2400s，防白烧 40 多分钟）与网关健康预检（`hermes status`，最多等 180s；10-20 实测撞上网关重启 `Gateway draining` 时整轮扫描 8 秒就废了）。
  - **取数段收尾产物兜底（2026-10-22 晚）**：拆段修复后仍有 3 轮实测失败——取数段 Agent 每轮都在"存档完成、写 `candidates.json` 之前"耗尽单轮工具调用预算（当天 6 条候选原文全部落盘，候选清单却始终没写出来），加工段按"文件不存在就跳过"指令空转出报，**6 条原文一条没进库**。两道修复：①取数段 prompt 把 `candidates.json` / `search-log.md` 从"收尾交付"改为"**每存档一条立即追加**"；②`run_scan.sh` 在取数段结束后（无论成败，`--stage process` 补跑同样）调用 `scripts/synth_candidates.py` 从当日 meta.json **无损合成**最小版候选清单（title/url/域名，`country_en`/`publish_date` 等语义字段留空，由加工段照抽取规则从存档原文补齐），脚本幂等、不联网、不编造、与已有 candidates.json 按 url 去重合并（Agent 已写的字段不会被覆盖）。
  - **生产更新**：`bash scripts/deploy_update.sh policy`（git pull + 依赖 + 自检）。仓库只有 clone 没有更新机制，生产不 pull = 闸门加固根本没生效——这正是 10-20 事故的载体。
  - **现场诊断**：`python scripts/diagnose_push.py` 一次打印：运行代码 commit（与远端是否一致）、当日范围锁/待审/已推标记、渠道配置（自动读 `~/.hermes/.env`，只配一个渠道时会提示"先在微信审阅"跑不通）、crontab 是否显式声明范围、调度日志尾部（自动找 `cron.log`/`scan.log`，有无 `scope=` 标记＝新旧版 run_scan.sh）。推送内容尾部也带 `代码 <commit>` 指纹，`.pushed` 标记带**范围与声明来源**。
- **Word 附件闭环（2026-10-20 现场新发现）**：Word 日报由 `report.py --docx` 生成，而这一步是 **Agent 照 prompt 执行**的——实测 10-20 的跑批漏了 `--docx`，只产出 csv/md/xlsx；而推送侧原本是 `if isfile(docx_path)` **静默跳过附件**，用户收到纯文本却以为带 Word，事后从推送记录里完全查不出。现在有两道确定性兜底 + 如实标注：
  - `report.py --ensure-docx`：Word 缺失才补生成（幂等），补不上返回 1 并打 `[WARN]`。`run_scan.sh` 跑批结束调一次，`push_report.py` 推送前再兜一次（覆盖手工跑/面板跑/Agent 直接跑等所有入口）。
  - 补不上就在正文页脚明确写「⚠️ 本次无 Word 附件」，不再无条件宣称"全文见 Markdown / Word / Excel"；`send_hermes` / `send_mail` 附件缺失时打 `[WARN]`。
  - `report.py --docx` 的异常一律优雅降级但留痕（不崩、不装没看见）。

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
- **国别主干与周轮转**：`policy_watch_countries` 六档是检索主干——`priority_1`/`regional`/`priority_2` 每日必扫，`border_neighbours`/`export_partners`/`state_level` 按 `weekly_shards`（ISO 星期）轮转，保证 7 天全覆盖而单日检索量不随国别数线性膨胀。**新增 policy 源必须挂到某个国别的 `source_ids`**，否则不会被主动定位到（2026-10 修正了 16 个孤儿源，含 ru-fsvps/jp-maff/eu-sante-sers 等）。**新增轮转档国别必须给 `shard`**（ISO 星期，空格分隔多天），既无 `shard` 又不在必扫档 = 永远扫不到。
