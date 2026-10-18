# 已知问题与限制 (known-issues.md)

> 体检沉淀与工单跟踪。本文件登记代码库**已识别但未修复**的问题,按"高/中/低"分档。
> 修复后从"未修复"移入"已修复"区,保留作为历史与回归参考。
> 上次体检: **2026-10-18** (基于 commit f7ae1c4)。

---

## 未修复

### 高 (建议本批处理)

#### A3 · `report.py` 用裸 `datetime.date.fromisoformat()` 解析 `event_date`,无 try/except
- 位置: `scripts/report.py:28-29`、`scripts/report.py:75-76`
- 现象: 任何入库记录的 `event_date` 被污染成 `2026-13-45` / `2026-10-XX` / `TBD` 时, `_date()` 抛 `ValueError`,`active` 列表构建中途崩; 进程退出码非零; `push_report.py` 拿不到 `data` → 返回 `[提示] 政策库为空`。生产上最难复现——单条脏数据足以让 `report.py` 当天空跑、推送缺位, 且和今天的"群里没消息"现象重合。
- 建议处理: `_date()` 包 try/except, 解析失败 skip 并打 `[WARN]`, 不要让单条坏数据拖垮整个 report。
- 复现命令: 手工往库里塞一条 `event_date="2026-99-99"`, 再 `python scripts/report.py --date 2026-10-18 --excel`。

### 中 (建议排期修)

#### B1 · `normalize.upsert()` 缺事务边界, 并发写有 read-modify-write 丢数据风险
- 位置: `scripts/normalize.py:407-429`
- 现象: `SELECT payload → 内存 merge → INSERT OR REPLACE` 没有显式 `BEGIN IMMEDIATE`。当前生产基本不并发 (cron 30-60 分钟一跑), 但 webapp "生成今日日报" 按钮触发的 `subprocess.run(report.py)` 会在另一进程再开 DB; 两个写者都读到同一行旧 payload, 各自 merge 后再 INSERT, 后写覆盖前写结果。
- 建议处理: 把整个 read-modify-write 用 `with con:` 包裹(自动 BEGIN/COMMIT), 或在 upsert 入口 `con.execute("BEGIN IMMEDIATE")`。

#### B2 · `event_id` 用 `effective_date`, `published_date` 走 `source.publish_date` → 重抽时 event_id 漂移
- 位置: `scripts/normalize.py:243-255`(`event_id_of`) + `scripts/normalize.py:268-279`(`published_date`)
- 现象: 第一轮抽取时只有 `event_date=2026-10-15`, 第二轮补回 `effective_date=2026-10-20` 后两次 event_id 不同, 库被拆成两条。规范里说 event_id 稳定可区分, 实际它把"先知 vs 后知"当成两条不同政策。
- 建议处理: event_id 的日期成分只取 `event_date`(公告日), 把 `effective_date` 移出 hash 键空间; 或反过来统一口径, 在 `event_id_of` 与 `published_date` 之间保持一致。

#### B4 · `webapp/api_generate` 用 `subprocess.run(cmd, shell=True)` 跑 env 变量
- 位置: `webapp/app.py:233-236`
- 现象: `RADAR_POLICY_GENERATE_CMD` 是 env 来源的字符串, shell=True 解析; 能写环境变量的人就能 RCE。生产 VPS 没人能改 env, 但 AGENTS.md 强调过"勿裸暴露公网"——如果哪天套反代或挂 docker 共享 env, 这就是 RCE 入口。
- 建议处理: `shell=False` + `shlex.split(cmd)`, 或用 `[sys.executable, "scripts/report.py", "--date", date, "--excel", "--docx"]` 列表形式; env 里的命令仅作为参数列表解析。

#### B5 · `webapp/api_events` 每次请求都 `from normalize import ...`, DB 连接每次新建
- 位置: `webapp/app.py:178-187`
- 现象: Python 复用模块缓存所以不是真性能问题, 但同进程内 `_geo_cache` 是 module-level 全局, DB 连接每次新建——并发稍微大一点, "一边写一边读" 窗口被 B1 放大。
- 建议处理: `from normalize import load_events, open_db` 提到模块顶部; 长期用 gunicorn worker 模型替换 Flask dev server。

### 低 (可选/优化)

#### C1 · `datetime.datetime.utcnow()` 在 Python 3.12+ 已弃用
- 位置: `scripts/normalize.py:47, 53`
- 现象: 3.12 起 deprecation warning, 未来 hard error。
- 建议处理: 改 `datetime.now(timezone.utc)`。

#### C2 · `_norm_date` 对 `2026-13-45` 这种坏值原样返回, 污染 ISO 比较
- 位置: `scripts/normalize.py:260-265`
- 现象: 无效日期被原样返回, 字典序比较时可能误判新旧; 与 A3 关联。
- 建议处理: 长度合法但语义非法时返回 None, 触发 A3 的 warn。

#### C3 · `upsert` 的 patch 过滤 `v not in (None, [], {})` 误把 `0`/`False` 当空值
- 位置: `scripts/normalize.py:411-412`
- 现象: 当前字段都是字符串, 影响小; 一旦新增数值/布尔字段就会丢。
- 建议处理: 改成白名单 `v is None or v == "" or v == [] or v == {}`, 显式可读。

#### C4 · `OFFICIAL_DOMAIN_SUFFIXES` 含 `"gov"` 通配, 跨检查清单时容易误放行
- 位置: `scripts/normalize.py:125-127`
- 现象: 实际 `.gov` 是受限 TLD, 第三方无法持有, 现实安全; 但理论上任何 `*.gov` 都被认官方, 跨检查时容易"伪官方站"漏检。
- 建议处理: 改成 `gov.us` 之类精确列表, 或让 `THIRD_PARTY_HINTS` 检查顺序在白名单之前(当前已经是)。

#### C5 · `collect.biz_now` 与 `normalize.biz_now` 一字不差地复制粘贴
- 位置: `scripts/collect.py:29-32`
- 现象: 时区口径漂移风险(虽然现在都是 +8h)。
- 建议处理: `from normalize import biz_now`, 删本地实现。

#### C6 · `collect.py` 序号 `seq` 在并发 collect 时会撞号, 文件互相覆盖
- 位置: `scripts/collect.py:67`
- 现象: 同一归档目录下两个进程并发时, 拿到相同 `seq`, 第二个写覆盖第一个。
- 建议处理: 用 `int(time.time()*1000)` 或 sha1(url)[:8] 替代递增号。

#### C7 · `sys.path` 注入 `templates/` 的写法与 `risk.py` / `report.py` 风格不一致
- 位置: `scripts/report.py:11-12`、`scripts/report_docx.py` 间接依赖
- 现象: 同一仓库里 import 风格分裂, 维护时容易漏改。
- 建议处理: 统一收口(例如在 `scripts/__init__.py` 或 `normalize.py` 里提供 `BASE_HELPERS` 一并 import)。

#### C8 · `prompts/policy-extraction.md:34` 的 quote 长度限制 (80 字) 与代码 (120 字) 不一致
- 位置: `prompts/policy-extraction.md:34` vs `scripts/normalize.py:328` / `docs/event-schema.md:28` / `TROUBLESHOOTING.md:43` / `AGENTS.md`
- 现象: 提示词比代码严 40 字, Agent 抽取时切得太短, 浪费可溯源信息; 实际运行时不会报错。
- 建议处理: 二选一——代码收紧到 80 字并改 `event-schema.md`, 或 prompt 放宽到 120 字。**优先放宽 prompt**。

#### C9 · `run_scan.sh:13` `set -u` 与 `[ -f "$HOME/.hermes/.env" ]` 在 `$HOME` 缺失时直接 exit
- 位置: `scripts/run_scan.sh:13, 19`
- 现象: 极端 cron 配置删除 HOME 时, `[ -f ]` 展开失败 → 整个脚本退出。当前 crontab.example 没显式设 HOME, 但 systemd 等环境里 HOME 可能 unset。
- 建议处理: 改为 `[ -n "${HOME:-}" ] && [ -f "$HOME/.hermes/.env" ] && . ...` 双层守卫。

#### C10 · `skills/daily-report/SKILL.md:29` 推送命令缺 `--message` / `--force` 用法
- 位置: `skills/daily-report/SKILL.md:26-30`
- 现象: 新人不知道"日报已发过怎么补发", 或 `run_scan.sh` 走 fallback 推送"待执行提醒"的 `--message` 路径。
- 建议处理: 在 skill 步骤 2 补一句: "日报已发过想补发: `push_report.py --date YYYY-MM-DD --force`; `run_scan.sh` 走 fallback 时传 `--message` 跳过日报汇总"。

---

## 已修复 (历史)

### 2026-10-18 体检 (commit f7ae1c4 起)

#### A1 · webapp 面板三处用 `datetime.date.today()`, 与生产 VPS UTC 时区冲突 → 修
- 位置: `webapp/app.py:21, 151, 157, 229`
- 修法: 删除 `import datetime`; `from normalize import DATA_DIR, REPO, biz_today` 增加 `biz_today`; 三处 `datetime.date.today().isoformat()` 全替换为 `biz_today()`。
- 影响: 面板"今日新增""生成今日日报"按钮按东八区日期落盘, 与其他脚本口径一致。

#### A2 · `push_report.py` 幂等命中时静默 return 0, 监控看不出 → 修
- 位置: `scripts/push_report.py:175-180`
- 修法: 日志前缀从 `[跳过]` 改为 `[WARN]`, 加注释说明外部监控只盯 exit code 时 WARN 前缀便于 grep。return code 仍保持 0(语义"非错误")。
- 影响: 今天`data/reports/.pushed/<日期>.flag` 已存在时, 日志会发 `[WARN]` 警告, 而不是悄悄看起来"成功了"。

#### B3 · `push_report.py` dry-run 不显示 flag 状态 → 修
- 位置: `scripts/push_report.py:202-214`
- 修法: dry-run 分支中, 当 `flag_path` 存在时, 在 `[dry-run] 将发送到: ...` 之前打一行 `[提示] 今日日报已推送过(标记: ...), --dry-run 仅预览不会重发; 要重发请加 --force`。
- 影响: Agent 看到 dry-run 输出时能立即知道今天已发过, 不会再被 `[dry-run] 将发送到 X` 误导以为没发过。
- 与"今日 bug"关系: 这条 + A2 是昨晚那条"日志显示是 dry-run(试运行)"的二次防御。即便 Agent 再被 `AGENTS.md:40` 的 dry-run 示例误导, 至少监控和 Agent 自己都能看出"flag 已存在"。

---

## 维护说明

- 体检频率: 每月一次或在出现生产事故后; 由本次负责人在本文件顶部更新"上次体检"日期。
- 优先级定义:
  - **高** — 已在生产或下一次 cron 必踩; 立即排期 (≤ 1 周)。
  - **中** — 生产偶发, 排期 (≤ 1 月)。
  - **低** — 优化项, 视精力而定; 不进生产事故响应通道。
- 修复闭环: 修复时从"未修复"移到"已修复"区, 保留位置/修法/影响三栏, 方便后续回归。
