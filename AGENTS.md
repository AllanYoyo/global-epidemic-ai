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
python scripts/push_report.py --date YYYY-MM-DD --dry-run   # 推送（企业微信/钉钉/SMTP）
REVIEW_CHANNEL=wechat python scripts/push_report.py --date YYYY-MM-DD  # 先审微信（评审核闸门阶段 1）
python scripts/push_report.py --date YYYY-MM-DD --review-approved  # 审核通过，发完剩余渠道（阶段 2）
python scripts/push_report.py --date YYYY-MM-DD --force      # 当日已推送过，强制重发
python webapp/app.py                                        # Flask 面板，默认 0.0.0.0:8000
scripts/run_scan.sh policy                                  # 定时扫描入口（am|pm|policy）
```

## 模块边界与运行方式

- scripts 内**同级导入**：risk.py `from normalize import ...`；report.py 和 webapp/app.py 靠 `sys.path.insert` 引入 scripts/ 与 templates/（Excel 样式工具在 `templates/base.py`）。因此一律从**仓库根目录**以 `python scripts/xxx.py` 运行；`run_scan.sh` 特意 `cd "$REPO"` 即为此（勿删）。
- 数据落地：`data/raw|events|reports/` 与 `database/`、`logs/` 均不入库（gitignore）。生产 VPS 用 `EPIDEMIC_DATA_DIR` / `EPIDEMIC_DB` 指到 repo 之外（/opt/global-epidemic-ai）。
- 环境变量：run_scan.sh 自动加载 `~/.hermes/.env`；推送渠道 `WECHAT_WEBHOOK` / `DINGTALK_WEBHOOK`(+`DINGTALK_SECRET` 加签) / `SMTP_*`；面板 `DASHBOARD_HOST/PORT`、`RADAR_GEO`、`RADAR_POLICY_GENERATE_CMD`、`RADAR_PUBLIC_URL`；Hermes 无头调用模板 `HERMES_RUN_CMD`（`{PROMPT}` 占位符，未配置时退化为待执行提醒推送）。
- **评审核闸门(2026-10-19 引入)**：用户在某渠道（微信/钉钉/邮箱）提出测试类请求时，强调"反馈必须先在原渠道完成再扩散到其它渠道"。`scripts/push_report.py` 新增 `REVIEW_CHANNEL` 环境变量（取值 `wechat`/`dingtalk`/`mail`）启用两段式：
  - **阶段 1**：`push_report.py --date X`（不传 `--review-approved`）→ 只发审阅渠道，写 `data/reports/.review-pending/<日期>.flag`（首行 `review_channel=<显示名>` 供阶段 2 读回），不写 `.pushed/`；其它渠道不推送。
  - **阶段 2**：用户在审阅端确认后再调 `push_report.py --date X --review-approved` → 把审阅渠道之外的剩余渠道发完，写 `.pushed/`，清理 `.review-pending/`。**审阅渠道以 `.review-pending` 标记里记录的渠道为准**（`REVIEW_CHANNEL` 只作兜底）：阶段 2 换会话/换机器执行时容易忘带环境变量，原实现会静默退化成"全量推送"，让审阅渠道重复收到日报、直接违反"先在原渠道完成再扩散"的红线——2026-10-19 已修。
  - **不启用**（默认，向后兼容）：不设 `REVIEW_CHANNEL` 时所有配置渠道一次性发出，行为与之前一致。`--message` 自定义文本不走闸门（cron fallback 推"待执行提醒"不能被卡）。
  - Hermes 在某渠道会话中复述"先在原渠道完成反馈"的口径时，应在该渠道对应的 `push_report.py` 调用中临时设置 `REVIEW_CHANNEL=<该渠道>`；用户在原渠道确认后再以 `--review-approved` 释放剩余渠道。

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
