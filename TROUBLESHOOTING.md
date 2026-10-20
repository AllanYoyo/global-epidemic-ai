# TROUBLESHOOTING.md · 常见问题排查

按故障类别组织，命令一律从**仓库根目录**执行。先看《ARCHITECTURE.md》第 1 节流程图确定故障在哪一环，再对号入座。

---

## 1. 某个信息源无法访问 / 连续超时

**现象**：`collect.py` 报 `urlopen` 超时（内置 30 秒）或 4xx/5xx；某站点连续数日没有产出。

排查顺序：

1. **本地浏览器直开**该源 URL。打不开 → 多半是站点改版或临时故障，等恢复即可（URL 是检索入口页，站点改版后由搜索重新定位）。
2. **浏览器能开、脚本超时** → 大概率反爬虫/IP 限流：
   - 换出口 IP（VPS 上的数据中心 IP 是常见黑名单对象）验证；
   - `collect.py` 带 `Mozilla/5.0 (compatible; GlobalEpidemicRadar/0.1; ...)` UA 与合理节奏，不要为绕反爬改成伪装浏览器 UA（与溯源立场冲突），改用降低频率或换该站的官方数据接口（RSS/通报列表页）；
   - WTO ePing、WOAH WAHIS 等大站偶发慢，属正常，核验环节会自动降级为 `unverified`/`single_source`，下一轮补核验。
3. **该源确实长期失效**：在 `config/sources.yaml` 中把条目 `url` 指向新入口，或下线该条目并在 PR 描述里注明原因（流程见 `CONTRIBUTING.md` 第 1 节）。
4. **不要为救一个源阻塞当天日报**：源挂掉只影响该源覆盖的记录，日报照常出，缺失部分下一轮自动补。

## 2. LLM / Hermes 环节出错

**现象 A：收到"待执行提醒"推送，扫描根本没跑。**
`HERMES_RUN_CMD` 未配置（或 `~/.hermes/.env` 未加载）。检查：

```bash
grep HERMES_RUN_CMD ~/.hermes/.env
# 应形如: HERMES_RUN_CMD='hermes run --prompt "{PROMPT}"'
```

`run_scan.sh` 会自动加载 `~/.hermes/.env`；用 cron 跑时还需确认 crontab 顶部补了 `PATH=` 行（见 `config/crontab.example`）。

**现象 B：Hermes 起了但某一段报错/超时。**
- 看 Hermes 自身日志定位是检索、抽取还是研判段失败；单段失败不会卡死整条线，失败记录留在对应状态（如待核实）下轮补跑；
- prompt 过长导致超限：减少单次检索的国家/主题面（按 `policy_watch_countries` 优先级分批），而不是砍词库；
- 密钥/配额类错误（4xx 余额、429 限流）：按 Hermes 平台侧的提示处理，本仓库不重试烧配额。

**现象 C：Hermes 跑完了但库里没有新记录。**
先确认它是否只完成了检索段（`data/raw/<日期>/` 有没有存档、`search-log.md` 有没有行），再查抽取段产出是否被 `normalize.py` 拒收（见第 3 节）。

## 3. 入库 / 日报生成失败

**normalize.py 报"缺少必填字段 / 枚举不合法"**：抽取产物违反数据合同。核对 `docs/event-schema.md`：枚举值必须用中文（收紧|放松|调整|恢复 等），`source.url` 与 `source.quote`（≤120 字）必填，缺 `event_date`/`effective_date` 也会被拒。**修上游 prompt 产物，不要放宽校验。**

**normalize.py 报"[旧闻拒收]"**：这是时间窗拦截在起作用——新增记录的发布时间（`source.publish_date`，缺省 `event_date`）早于"今天-N 天"（默认 7 天，`POLICY_MAX_AGE_DAYS` 覆盖）。常年挂网的官方公告页会被检索撞到，属预期拒收。确属新线索（如官方补发）先回页面核实发布日期；历史回补/库重建重放用 `--max-age-days <N>`（`0` 为关闭）。**不要把 0 当日常扫描参数，旧闻入库会被日报当成"今日新增"。**

**report.py 报 ImportError**：`from normalize import ...` 失败说明没从仓库根目录运行。统一用：

```bash
python scripts/report.py --date 2026-09-26 --excel --docx
```

**Excel/Word 没生成**：缺 openpyxl（报告降级 CSV）或 python-docx（跳过 Word）属预期优雅降级，看 stderr 提示；需要全格式就 `pip install -r requirements.txt`，**不要为此加硬依赖**。

**日报是空的/记录很少**：先确认库里当天有数据：

```bash
python -c "import sqlite3;con=sqlite3.connect('database/epidemic.db');print(con.execute(\"select event_date,count(*) from events where record_type='policy' group by 1 order by 1 desc limit 7\").fetchall())"
```

没有 → 是上游采集/入库问题（回到第 2 节）；有 → 检查 `--date` 参数与时区（见第 6 节）。

**误入库/需要整行覆盖**：重复 event_id 默认按合并更新（空值与 `DEFAULT_SENTINELS` 兜底值不覆盖已有核验/研判结论）；确要整行替换：`python scripts/normalize.py --input <json> --db --replace`。

## 4. 推送失败 / 重复 / 漏发

| 现象 | 处理 |
|---|---|
| 提示"未配置推送渠道"退出码 1 | `WECHAT_WEBHOOK` / `DINGTALK_WEBHOOK` / `SMTP_*` 一个都没配；配哪个发哪个 |
| 钉钉发不出去 | 安全设置选"加签"时必须同时配 `DINGTALK_SECRET`；webhook 过期需在钉钉群机器人设置里重建 |
| 同一天重复执行只发了一次 | **这是幂等特性**：成功后写 `data/reports/.pushed/<日期>.flag`；确认有新内容要重发用 `--force`（渠道会话里补发用 `--force --review-channel <渠道>`，仍只发该渠道） |
| 提示"未指定推送范围"退出码 2，日报没发出去 | **默认不扩散**：必须显式给 `--review-channel <渠道>`（渠道会话阶段 1）、`--review-approved`（阶段 2）或 `--broadcast`（定时全量）之一。这是刻意的 fail-closed，防止"忘了指定渠道"被当成"发所有渠道" |
| 提示"审阅渠道无法识别"退出码 1 | `--review-channel` 的值认不出。合法值：`wechat` / `dingtalk` / `mail`，也认中文别名（企业微信/钉钉/邮箱）与简写（wx/wecom/email）。**认不出时脚本中止而不是发到所有渠道** |
| 提示"--broadcast 被拒绝/被推送范围锁拒绝"退出码 1 | `--broadcast` 不再自声明：全量推送必须先由**调度路径**声明范围——`scripts/run_scan.sh <模式> --broadcast`（推荐）或 `python scripts/push_scope.py --date <日期> --broadcast --source manual`。若提示"锁为只发 X"，说明本轮已声明为渠道会话，先在原渠道确认，再用 `--review-approved` |
| 明明只想发微信，钉钉群也收到了 | ①先跑 `python scripts/diagnose_push.py`（只读诊断）：看当日范围锁/待审标记、crontab 是否显式声明范围、调度日志有无 `scope=` 标记；②查是否用了不带 `--review-channel` 的命令（现在默认不推送，不会漏发）；③查该日期是否已有 `.review-pending` 待审标记（有则任何推送都只发审阅渠道）；④查是否走了 `--broadcast`；⑤**确认生产上跑的是新代码**：`git -C /opt/global-epidemic-ai/repo rev-parse --short HEAD`，与 `git ls-remote origin policy` 不一致就先 `bash scripts/deploy_update.sh policy` |
| 群里只有"待执行提醒"，没有日报 | `HERMES_RUN_CMD` 未配置或 `~/.hermes/.env` 没配；`run_scan.sh` 降级路径只发提醒不发日报 |
| 想先看效果再发 | `python scripts/push_report.py --date <日期> --dry-run` 只打印不发送 |
| 邮件没到 | `SMTP_PORT` 默认 465 走 SSL；Tuta 等网页邮箱不经本脚本，由 Hermes 直接调用其 webmail 技能 |

## 5. 网页面板问题

| 现象 | 处理 |
|---|---|
| 页面白屏/数据不刷新 | 先 Ctrl+F5 强刷；仍不行查服务进程 |
| 起服务报端口占用 | 换端口：`DASHBOARD_PORT=8001 python webapp/app.py`（默认 0.0.0.0:8000；勿裸暴露公网） |
| 地图没有圆点 | 圆点只画能定位到国家的记录；量大时可查 `data/geocache.json` 是否为空。Nominatim 限流时稍后重试，或 `RADAR_GEO=0` 整体关闭地理编码（地图会缺点位，其余功能正常） |
| events API 返回空 | 合同如此：必须显式传 `record_type=policy` 才返回数据 |
| 点"生成今日政策日报"失败 | 展开页面"运行日志"看返回码；默认执行 `report.py --excel --docx`，报错原因多半在第 3 节（路径/依赖）；要自定义生成流程用 `RADAR_POLICY_GENERATE_CMD`（旧的 `RADAR_GENERATE_CMD` 不再覆盖政策命令） |
| 面板顶部链接打不开 | 面板对外链接由 `RADAR_PUBLIC_URL` 控制，未配置时消息尾部不附链接 |

## 6. 时间与部署

- **日报日期错位/扫描时间不对**：cron 服务器时区必须是 Asia/Shanghai（`timedatectl` 核对）；cron 精简的 PATH 可能找不到 `python3`，在 crontab 顶部手工补 `PATH=` 行。
- **生产代码没更新**（10-20 事故的头号嫌疑）：VPS 上是 `/opt/global-epidemic-ai/repo` 这个 clone 的工作树，**不会自动 pull**。核对 `git -C /opt/global-epidemic-ai/repo rev-parse --short HEAD` 与 `git ls-remote origin policy`；不一致就 `bash scripts/deploy_update.sh policy`。推送内容尾部的 `代码 <commit>` 就是现场版本指纹。
- **crontab 里的推送范围声明**：`run_scan.sh` 在 cron 上下文会自动按定时任务全量处理，但**建议显式写** `run_scan.sh <模式> --broadcast`（`config/crontab.example` 已更新）——显式声明后行为不依赖上下文识别。`python scripts/diagnose_push.py` 会检查这一点。
- **生产数据目录**：生产 VPS 应设 `EPIDEMIC_DATA_DIR` / `EPIDEMIC_DB` 指到 repo 之外（如 `/opt/global-epidemic-ai`）；如果日报里没数据但数据目录里有文件，先确认进程实际读的是哪个路径（默认是 repo 内 `data/`、`database/`）。
- **库损坏/误删需要重建**：`data/events/<日期>/*.json` 是入库镜像，用 `python scripts/normalize.py --input <json> --db --max-age-days 0` 逐个重放即可重建（重放的是历史镜像，必须关掉旧闻拦截）；日常请备份 `database/epidemic.db` 与 `data/`。
- **`.pushed` 标志文件导致不推送**：确属当天已发过的记录，删除 `data/reports/.pushed/<日期>.flag` 或直接用 `--force`（推荐后者）。

---

## 快速定位口诀

| 症状 | 先查 |
|---|---|
| 群里只有"待执行提醒" | `HERMES_RUN_CMD` 与 `~/.hermes/.env` |
| 收不到推送 | 渠道环境变量 → 加签 → 幂等 flag（`--force`） |
| 日报格式缺失 | openpyxl / python-docx 是否安装 |
| 日报没内容 | 库里当天是否有 policy 记录 → 上溯采集段 |
| 校验报错 | 对照 `docs/event-schema.md`，修抽取产物而非放宽校验 |
| 面板/接口为空 | 是否显式 `record_type=policy` |
