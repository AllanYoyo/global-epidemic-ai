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
- **影响加权合同**（risk.py 强校验，须与 prompts/policy-impact.md 一致）：
  `impact_score = trade*0.35 + biosecurity*0.30 + response*0.25 + alignment*0.10`；≥3.5 高影响 / ≥2.0 中影响 / 其余低影响；高→立即关注、中→持续观察、低→常规记录。四维齐全时 score/level/focus 由代码推导，不得手写冲突值。
- **枚举值是中文**：action_type 收紧|放松|调整|恢复；policy_domain animal|plant|both|trade|measures；impact_type 约束|机会|中性；impact_level 高影响|中影响|低影响；china_relevance 直接涉及中国|间接影响|暂无明显关联；verification_status verified|single_source|unverified|false_positive|merged。
- **可溯源硬要求**：`source.url` 与 `source.quote`（≤120 字）必填；原文没有的信息填 null，严禁编造。

## 常用命令

无测试套件、无 linter 配置。依赖仅 openpyxl / python-docx / flask，全部**优雅降级**（缺 openpyxl 报告降级 CSV，缺 python-docx 跳过 Word，不要为此加硬依赖）。

```bash
pip install -r requirements.txt
python scripts/collect.py --url <url> --title <标题>  # 原文存档到 data/raw/（可溯源链第一环）
python scripts/normalize.py --input <json> --db     # 入库（支持 - 读 stdin）
python scripts/risk.py --event-id <id> --dim trade=4 --dim biosecurity=3,...   # 影响写回
python scripts/report.py --date YYYY-MM-DD --excel --docx   # 日报（--policy 已废弃为 no-op）
python scripts/push_report.py --date YYYY-MM-DD --dry-run   # 推送（企业微信/钉钉/SMTP）
python webapp/app.py                                        # Flask 面板，默认 0.0.0.0:8000
scripts/run_scan.sh policy                                  # 定时扫描入口（am|pm|policy）
```

## 模块边界与运行方式

- scripts 内**同级导入**：risk.py `from normalize import ...`；report.py 和 webapp/app.py 靠 `sys.path.insert` 引入 scripts/ 与 templates/（Excel 样式工具在 `templates/base.py`）。因此一律从**仓库根目录**以 `python scripts/xxx.py` 运行；`run_scan.sh` 特意 `cd "$REPO"` 即为此（勿删）。
- 数据落地：`data/raw|events|reports/` 与 `database/`、`logs/` 均不入库（gitignore）。生产 VPS 用 `EPIDEMIC_DATA_DIR` / `EPIDEMIC_DB` 指到 repo 之外（/opt/global-epidemic-ai）。
- 环境变量：run_scan.sh 自动加载 `~/.hermes/.env`；推送渠道 `WECHAT_WEBHOOK` / `DINGTALK_WEBHOOK`(+`DINGTALK_SECRET` 加签) / `SMTP_*`；面板 `DASHBOARD_HOST/PORT`、`RADAR_GEO`、`RADAR_POLICY_GENERATE_CMD`、`RADAR_PUBLIC_URL`；Hermes 无头调用模板 `HERMES_RUN_CMD`（`{PROMPT}` 占位符，未配置时退化为待执行提醒推送）。

## 约定与注意事项

- 面向用户的文案（docstring、CLI 输出、校验报错、日报、文档）**一律中文**；代码注释也用中文，风格贴近现有 `%` 格式化、stdlib 优先（urllib 而非 requests）。
- 开发在 Windows（注意 CJK 字体：templates/base.py 按 platform 选 Microsoft YaHei / Noto Sans CJK SC），生产是 Linux VPS。
- webapp 面板只读 + 一键生成日报；events API 需显式 `record_type=policy` 才返回数据。勿裸暴露公网。
- 地理编码走 OSM Nominatim，缓存于 `data/geocache.json`，`RADAR_GEO=0` 关闭；同国多点做确定性偏移。
- cron 部署要求时区 Asia/Shanghai，且需在 crontab 手工补 PATH（见 config/crontab.example）。
- 提交信息用 conventional commits（feat/fix/docs/chore(scope)），详见 git log 风格。
- 敏感区改动前先读：`docs/event-schema.md`、`prompts/policy-impact.md`（研判口径）、`config/sources.yaml`（源分级 tier 与 schedule 档位）。
