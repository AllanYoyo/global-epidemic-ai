# 已知问题与限制 (known-issues.md)

> 体检沉淀与工单跟踪。本文件登记代码库**已识别但未修复**的问题,按"高/中/低"分档。
> 修复后从"未修复"移入"已修复"区,保留作为历史与回归参考。
> 上次体检: **2026-10-20** (基于 2026-10-19 专项复审后的推送范围锁改造与 10-20 钉钉误推事故复盘)。

---

## 未修复

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

#### B6 · `normalize.py` 退出码: 部分旧闻拒收即返回 1(语义待定)
- 位置: `scripts/normalize.py` `main()` 末尾 `return 1 if (failed or stale) else 0`
- 现象: **旧闻拒收是检索撞到常年挂网旧公告的正常预期行为**, 但与"校验失败"共用退出码 1。日常扫描几乎天天会拒收一两条旧公告 → 天天 exit 1, Agent/脚本容易把"部分成功"误读成"整批失败"而中断后续流程。
- 建议处理: 拆成两个退出码(如 0=全成功 / 1=有校验失败 / 2=有旧闻拒收但入库成功), 或旧闻拒收不参与退出码、只打提示。**本项是接口语义变更, 需先与调度侧对齐再改。**

### 低 (可选/优化)

#### C1 · `datetime.datetime.utcnow()` 在 Python 3.12+ 已弃用
- 位置: `scripts/normalize.py:47, 53`
- 现象: 3.12 起 deprecation warning, 未来 hard error。
- 建议处理: 改 `datetime.now(timezone.utc)`。

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

#### C11 · 第三方站点记录的"降 tier 3"与"整条拒收"口径曾不一致
- 位置: `AGENTS.md` / `docs/event-schema.md` / `scripts/normalize.py` `check_source_domain`
- 现象: 2026-10-19 前, 文档写"强制降 tier 3"(暗示保留为线索), 实现却是"降 tier 3 + 校验失败"= 整条拒收, 错误文案还写"[线索] ... 已将 tier 降为 3", 自相矛盾。
- 现状: 行为**保持拒收**(与 `skills/global-policy-search/SKILL.md` 红线"第三方命中不存档、不入库"一致, 且不重新打开 2026-10 堵住的"伪 tier 1"口子), 文案与文档已统一为"整条不入库"。
- 待办: 若业务上确实要保留 tier 3 线索条目, 需改行为并同步 event-schema.md / 两个 prompt / 报告展示层 —— 属口径变更, 不宜顺手改。

#### C12 · `webapp` 地理编码把"查询失败"也写进缓存
- 位置: `webapp/app.py:87`(`cache[country_en] = pos`, 失败时 `pos=None`)
- 现象: 一次网络抖动/限速导致查不到坐标, 结果被永久缓存成 `None`, 该国所有点此后都不显示在地图上, 且没有任何日志。
- 建议处理: 失败结果不写缓存(或短 TTL), 并打 `[WARN]` 日志。

---

## 已修复 (历史)

### 2026-10-20 推送范围锁改造 (G1 加固之后)

#### G2 · 推送范围由 Agent 临场判断 → 手动补跑被当成定时任务, 日报又发到钉钉(用户实测)→ 改为范围锁
- 位置: `scripts/run_scan.sh`(参数解析/推送范围声明/prompt 组装);
  `scripts/push_report.py`(`main()` 范围锁判定/阶段 2 早退/`brief_text` 指纹);
  新增 `scripts/push_scope.py`(范围锁)、`scripts/buildinfo.py`(代码指纹)、
  `scripts/diagnose_push.py`(现场诊断)、`scripts/deploy_update.sh`(生产更新);
  `skills/daily-report/SKILL.md`; `skills/global-policy-search/SKILL.md`;
  `AGENTS.md`; `README.md`; `TROUBLESHOOTING.md`; `ARCHITECTURE.md`; `config/crontab.example`
- 用户实测反馈(2026-10-20): 07:30 定时扫描因 Gateway draining 失败 → 手动补跑成功
  入库 1 条(欧盟修订高致病性禽流感受影响成员国名单)→ **又推送到钉钉**。
  用户指认: "闸门逻辑在脚本里可能没生效, 需要在代码端检查 `run_scan.sh` 是否正确传递
  `REVIEW_CHANNEL`"。
- **根因(三条, 缺一不可)**:
  1. **判断权错位**: G1 修完后闸门虽然 fail-closed, 但"本轮是渠道会话还是定时任务"
     这个**客观事实**仍被写成 prompt 里的二选一, 交给 Agent 临场判断。而
     `run_scan.sh` 是 cron 与手动补跑**共用**的入口 —— 手动补跑在 prompt 眼里就是
     "定时任务那条路", 于是 Agent 选了 `--broadcast`。
  2. **`run_scan.sh` 根本没有 REVIEW_CHANNEL 传递机制**(用户的指认属实): 渠道信息
     只以自然语言出现在 prompt 里, 靠 Agent 照着拼命令; 脚本本身既不读也不传
     `REVIEW_CHANNEL`, 也没有任何地方承载"本轮是谁发起的"。
  3. **`--broadcast` 是"自声明"的**: 谁喊一声都能全量, 脚本无从判断这是不是定时任务。
     G1 修完的只是"忘了指定就不推", 没堵住"随便指定成 broadcast 就全量"。
- **修法(把判断权从 Agent 手里拿走)**:
  - **推送范围锁**: 新增 `scripts/push_scope.py`, 把范围落盘成
    `data/reports/.push-scope/<日期>.lock`(`scope=review|channel=…` 或 `scope=broadcast`)。
    `push_report.py` **读锁执行**:
    · lock=review → 本轮只允许发该渠道, `--broadcast` 被**拒绝**(退出码 1);
    · lock=broadcast → 才放行全量;
    · 没有锁 → `--broadcast` 被拒绝(全量只能由调度路径声明)。
    阶段 1 自己写 review 锁, 阶段 2 发完清锁。
  - **`run_scan.sh` 显式化**: 新增 `--review-channel <渠道>` / `--broadcast` 互斥参数,
    **先写范围锁再调 Hermes**, 并 `export REVIEW_CHANNEL` 真正传给下游;
    prompt 改成**确定性指令**(不再有"若是…若是…"), 杜绝 Agent 临场二选一。
    两个都不给时: cron 上下文(祖先进程含 cron/crond, 刻意不含 systemd)自动按定时
    任务处理; 其它上下文**不扩散**(`--broadcast` 一律拒绝)。`config/crontab.example`
    同步加 `--broadcast`。
  - **阶段 2 无待审记录即拒绝**: 原实现"警告一下照常全量发", 是又一个
    "忘了 → 发到所有渠道"的口子。
  - **锁与参数冲突即拒绝**: 锁是微信而命令行指定钉钉 → 拒绝(同一轮不得改投其它渠道)。
  - **可观测**: 推送内容尾部带 `代码 <commit>` 指纹; 新增 `scripts/diagnose_push.py`
    (只读)打印代码版本(与远端是否一致)/范围锁/待审与已推标记/渠道配置/crontab 是否
    显式声明范围/cron 日志尾部(有无 `scope=` 可区分新旧版 run_scan.sh);
    新增 `scripts/deploy_update.sh` 消除"生产不 pull = 加固没生效"这个根因载体。
- 回归: `scripts/test_smoke.py::PushScopeLockTests` 新增 18 条(范围锁读写/CLI/四条拦截/
  放行/消息路径落锁/dry-run 不落锁/诊断脚本/编排层静态断言), `PushReportReviewGateTests`
  相应改写(`--broadcast` 需先有全量声明; 阶段 2 无待审记录改为拒绝); 自测 108 → 126 条全绿。
- **未闭环(需现场确认)**: 本地无生产环境, 无法直接证明 10-20 那次是"旧代码"还是
  "新代码 + Agent 选了 --broadcast"。两条路径都已封死, 且新增 `diagnose_push.py`
  可在 VPS 上一条命令给出答案; 另需确认生产 crontab 是否已加 `--broadcast`。

### 2026-10-20 生产现场复盘 (G2 落地后)

#### G5 · "只发微信审阅"在生产上根本跑不通 —— 脚本发不进用户那个微信会话 → 新增 Hermes 投递型渠道
- 位置: `scripts/push_scope.py`(渠道表 / 别名 / 可用性判定);
  `scripts/push_report.py`(`send_hermes` / `_build_channels` / 审阅渠道可用性判定);
  `scripts/diagnose_push.py`(渠道报告); `skills/daily-report/SKILL.md`;
  `skills/global-policy-search/SKILL.md`; `AGENTS.md`; `README.md`; `TROUBLESHOOTING.md`
- 现场事实(SSH 到生产查证): `.env` 里**没有** `WECHAT_WEBHOOK`、没有 SMTP, 只有
  `DINGTALK_WEBHOOK`; 用户与 Hermes 说话的"微信会话"是 **Hermes 的个人微信通道**
  (`WEIXIN_TOKEN` / `WEIXIN_HOME_CHANNEL` / `WEIXIN_BASE_URL`)。也就是说:
  **脚本自己 POST 永远发不进用户那个会话** —— `--review-channel wechat` 只会报
  "审阅渠道 企业微信 未配置"并中止。用户反复要求"先发微信审阅"却总看到钉钉收到,
  根子在这里: **审阅渠道本身是空的**。
- 修法: 借 Hermes 自己的投递通道(它复用网关凭据, 不走 LLM):
  - 新增 **Hermes 投递型渠道** `weixin`(微信)/ `feishu`(飞书), 由
    `hermes send --to <平台> --file -` 投递(正文走 stdin; docx 存在时追加
    `MEDIA:<path>` 顺带发 Word 报告)。
  - **渠道名正名**: "微信" → `weixin`(个人微信/Hermes); "企业微信" → `wechat`
    (群机器人 webhook)。原先 "weixin" 是"拼错的例子", 现在是合法渠道。
  - **可用性判定分两类**: webhook 型看环境变量, Hermes 型看 `hermes` CLI 是否在
    PATH; 不可用时明确报错(不再静默退回"发所有渠道")。
  - **Hermes 渠道默认不参与定时全量**(`HERMES_PUSH_CHANNELS` 才纳入), 免得日报
    突然开始刷屏用户微信; 但 `--review-channel weixin` 随时可用 —— 这正是"先在
    微信审阅"要的效果。
  - `diagnose_push.py` 改用 `hermes send --list` 判定真实可投递渠道, 不再误报
    "先在微信审阅跑不通"。
- 回归: `scripts/test_smoke.py::PushHermesChannelTests` 新增 12 条(别名/正名/
  只发微信/写 review 锁/默认全量不含微信/HERMES_PUSH_CHANNELS 打开才含/
  阶段 2 不重复发微信/hermes 缺失即中止/send_hermes 走 stdin 且带附件/失败抛错);
  另修正 3 条老用例(把 `weixin` 当拼错名字的样本); 自测 130 → 142 条全绿。

#### G6 · 人工在普通 shell 里跑 push_report.py 必然"未配置任何推送渠道" → 脚本自己补齐 .env
- 位置: `scripts/push_scope.py`(`load_hermes_env`); `scripts/push_report.py`(`main()` 开头调用);
  `scripts/diagnose_push.py`(改用共享实现)
- 现象(生产现场实测): `python3 scripts/push_report.py --date 2026-10-20 --broadcast --dry-run`
  输出「未配置任何推送渠道(WECHAT_WEBHOOK / DINGTALK_WEBHOOK / SMTP_*)」——
  **渠道配置在 `~/.hermes/.env` 里, 而只有 `run_scan.sh` 会 source 它**。
  于是: cron 路径正常(run_scan.sh 加载过), Hermes agent 路径正常(继承网关进程环境),
  唯独人工 shell 路径是空的 —— 白跑一趟, 还会让人误判"生产没配渠道"。
- 修法: `push_scope.load_hermes_env()` 从 `~/.hermes/.env` 补齐**推送渠道相关**
  变量(只补 `PUSH_ENV_KEYS` 那几个键, 已存在的同名变量不覆盖, 不把其它密钥带进
  子进程); `push_report.py` 在 `main()` 开头调用, 让任何入口的渠道集合一致。
- 回归: 自测 142 → 143 条全绿(新增"人工 shell 场景下渠道能从 .env 补齐"一条)。

#### G7 · Word 日报"生成靠 Agent 记性 + 附件缺失静默跳过" → 推送出去的可能只有正文
- 位置: `scripts/report.py`(`ensure_docx` / `--ensure-docx`); `scripts/push_report.py`
  (`brief_text` / `send_hermes` / `send_mail` / `main()` 日报分支); `scripts/run_scan.sh`(跑批结束兜底)
- 现场证据(2026-10-20 端到端验证时发现): 生产 `data/reports/` 里 10-19 有
  `.md/.xlsx/.csv/.docx` 四件套, **10-20 只有 `.md/.xlsx/.csv` —— 缺 `.docx`**。
  追因: Word 日报由 `report.py --docx` 生成, 而这一步是 **Agent 照 prompt 执行**的
  (`run_scan.sh` 只把 `report.py --date X --excel --docx` 写进 prompt), Agent 这次
  漏了 `--docx`; 而推送侧是 `if docx_path and os.path.isfile(docx_path)` ——
  **文件不存在就静默跳过附件**, 一声不吭。
- 后果(比"少个附件"严重): 用户收到的是纯文本, 却以为带 Word; **事后从推送记录里
  完全看不出来** —— 邮箱/Hermes 通道都没留下"附件缺失"的痕迹, 等用户发现时已经
  过了好几天, 而且**每一次推送都这样**。
- 修法(两道确定性兜底 + 如实标注):
  - `report.py` 新增 `ensure_docx(date)`: Word 缺失时补生成一次, 已有则幂等不动;
    补不上返回 1 并打 `[WARN]`。新增 `--ensure-docx` 入口供脚本调用。
  - `run_scan.sh` 跑批结束(Hermes 执行完、推送之前)调一次 `--ensure-docx` —— 修
    "扫描产物不完整"这个根因。
  - `push_report.py` 推送前再兜一次 —— 覆盖手工跑、面板跑、Agent 直接跑等所有入口;
    补不上就在正文页脚明确写「⚠️ 本次无 Word 附件」, 不再无条件宣称
    "全文见 Markdown / Word / Excel"。
  - `send_hermes` / `send_mail` 附件缺失时打 `[WARN]`, 不再静默。
  - `report.py` 的 `--docx` 分支把 `ImportError` 以外的异常也接住, 优雅降级但留痕。
- 回归: 自测 143 → 150 条全绿(新增 `ReportAttachmentTests` 7 条: 幂等/补生成/补不上/
  推送前补齐/正文如实标注/页脚标注/渠道侧告警)。

#### G3 · 诊断/更新脚本自身有三个坑, 现场第一次跑就卡住 → 全部修掉
- 位置: `scripts/deploy_update.sh`; `scripts/diagnose_push.py`; `scripts/buildinfo.py`;
  `.gitignore`; `scripts/push_report.py`(`.pushed` 标记)
- 现场: 10-20 在生产 VPS 上第一次跑 `bash scripts/deploy_update.sh policy` +
  `python3 scripts/diagnose_push.py`, 暴露三个问题:
  | # | 现象 | 后果 |
  |---|---|---|
  | ① | 更新脚本用 `git status --porcelain` 判脏, **未跟踪文件也算** | `data/geocache.json` 是运行时缓存, 永远未跟踪 → 更新**永远做不成** → 闸门加固永远落不到生产(这正是 G2 的根因载体, 差点自己复发) |
  | ② | 诊断脚本只查 `cron.log`, 生产实际日志叫 `scan.log` | 关键证据(日志里的 scope 标记)读不到, 等于没诊断 |
  | ③ | 诊断脚本只看当前 shell 的环境变量, 不读 `~/.hermes/.env` | 手动跑时误报"企业微信未配置" —— 差点据此误判生产配置 |
- 修法:
  - 更新脚本只拦**已跟踪文件**的改动(`--untracked-files=no`), 未跟踪文件仅提示;
    `data/geocache.json`、`scripts/_audit*.py` 补进 `.gitignore`。
  - 诊断脚本按候选名自动找日志(`cron.log` / `scan.log` / `run_scan.log`),
    `--log` 可显式指定; 并自动读取 `~/.hermes/.env` 补齐渠道配置(已存在的同名
    变量不覆盖), 报告里区分"当前 shell"与".env 补齐"。
  - `buildinfo.is_dirty()` 只看已跟踪文件(否则 geocache 让版本指纹永远"+脏"),
    新增 `untracked_files()` 单独列示。
  - `.pushed` 标记补记**范围与声明来源**(`范围=全量(定时任务) · 声明来源=...`),
    下次事故复盘能一眼定性: 定时任务广播的, 还是有人手动声明的。
- 回归: 自测 126 → 128 条全绿(新增诊断读 .env / 自动找 scan.log 两条)。

#### G4 · 全量推送的"声明"仍可被随手做掉(现场锁 source=manual 暴露)→ 声明须写明依据 + 须发生在 cron 上下文
- 位置: `scripts/push_scope.py`(`--broadcast` 的 `--source` 校验); `scripts/run_scan.sh`
  (`--broadcast` 的 cron 上下文校验)
- 现场证据: 10-20 生产上当日范围锁为 `scope=broadcast · source=manual ·
  declared_at 12:34:47`, 紧接着 12:34:48 推送落钉钉。`manual` 正是
  `push_scope.py --source` 的**默认值** —— 说明有人/有 Agent 直接敲了
  `push_scope.py --broadcast` 就把那一轮变成全量。
  **G2 把"广播需要声明"做成了硬约束, 却没管住"声明本身谁都能做"**:
  Agent 只要照着文档敲一条 `push_scope.py --broadcast`, 就能绕开闸门。
- 修法:
  - **全量声明必须写明依据**: `push_scope.py --broadcast` 不再接受默认来源,
    `--source` 留空即拒绝(退出码 1), 必须写清 `run_scan.sh:policy:cron自动识别`
    或 `manual:运维补发` 这类依据, 并落进锁里供事后审计。
  - **全量声明必须发生在 cron 上下文**: `run_scan.sh --broadcast` 在**非 cron
    上下文**(手动/渠道会话触发)一律拒绝并退出码 2 —— 手动补跑与 cron 共用入口,
    允许它自称定时任务, 就等于把"谁发起的"又交回给调用方。
    人工运维确需全量补发, 走 `push_scope.py --broadcast --source manual:运维补发`
    + `push_report.py --broadcast` 这条**显式留痕**的路。
- 回归: 自测 128 → 130 条全绿(全量声明缺来源被拒 / 编排层静态断言)。

### 2026-10-19 专项复审 (commit 0459442 之后)

#### G1 · 评审核闸门有 5 条漏法, "只发微信"实际会发到钉钉(用户实测)→ 全部 fail-closed
- 位置: `scripts/push_report.py`(`main()` 渠道判定 / `--message` 分支 / 幂等早退块);
  `scripts/run_scan.sh`(am/policy 档 prompt、fallback 提醒); `skills/daily-report/SKILL.md`;
  `skills/global-policy-search/SKILL.md`; `AGENTS.md`; `README.md`; `TROUBLESHOOTING.md`
- 用户实测反馈: "测试环节只把内容推送到微信, 刚刚的测试还是发了钉钉群"。
  逐条复现后确认 5 条漏法(每条都用探针实跑, 不是推测):

  | # | 漏法 | 复现结果 | 后果 |
  |---|---|---|---|
  | ① | 渠道名写错/写中文时**静默丢弃** | `REVIEW_CHANNEL=weixin` → 发出 `['wechat','dingtalk']` | 等价于"没设闸门"→ 全量。中文"企业微信"、简写"wx" 最常踩 |
  | ② | `--force` 绕过待审保护 | 阶段 1 后 `--force` 忘带参数 → `['wechat','dingtalk']` | 钉钉收到 |
  | ③ | `--message` 完全绕过闸门 | 带 `REVIEW_CHANNEL=wechat` 仍 `['wechat','dingtalk']` | 钉钉收到 |
  | ④ | 待审期间 `--message` 仍群发 | 阶段 1 后 `--message` → `['wechat','dingtalk']` | 钉钉收到 |
  | ⑤ | **默认就是全量**(结构性) | 不带任何参数 → `['wechat','dingtalk']` | 任何一次"忘了指定渠道"都会扩散 |

- 根因: 闸门是 **opt-in + fail-open** 设计 —— ①脚本层靠环境变量 opt-in, 认不出的值被
  静默丢弃(后果是"发所有渠道"而不是"报错不发"); ②`--force` / `--message` 两条路径
  直接绕过; ③**编排层默认全量**(`run_scan.sh` 的 policy prompt 与两个 skill 的推送
  步骤写的都是"推送到已配置渠道", "只发审阅渠道"只是附带条件); ④"本轮来自哪个渠道"
  这个信息没有任何地方承载, 全靠 Agent 每次记得手动设环境变量。
- 修法(脚本层 + 编排层一起改):
  - **脚本层 fail-closed**: 认不出的渠道名 → **报错退出**(不再静默丢弃); 新增
    `--review-channel` 显式参数(优先于环境变量, 命令行里可见可审计)并把中文别名
    (企业微信/钉钉/邮箱)与简写(wx/wecom/email)纳入白名单; `--broadcast` 与
    `--review-channel` 互斥。
  - **消除两条旁路**: 待审期间(存在 `.review-pending`)任何推送, 含 `--force` 与
    `--message`, 一律只发审阅渠道; `--broadcast` 会被待审标记挡下。
  - **默认不扩散(堵住 ⑤)**: 日报推送必须显式给出 `--review-channel`(阶段 1)/
    `--review-approved`(阶段 2)/`--broadcast`(定时全量)之一, 否则**不推送**并以
    退出码 2 打印三种写法。理由: "忘了指定"的后果应是"没发"(用户马上发现),
    而不是"发到所有渠道"(要等别人投诉才发现)。定时任务入口 `run_scan.sh` 已改为
    显式 `--broadcast`, 不受影响。
  - **编排层**: `run_scan.sh` 的 am/policy 档 prompt 把"推送前**必须先判定本轮是否
    来自单一渠道会话**"写成前置步骤并给出两条分支命令; `daily-report` skill 把
    "判定渠道"提为推送的第 0 步; `global-policy-search` skill 同步。
- 回归: `scripts/test_smoke.py::PushReportReviewGateTests` 新增 12 条
  (含 5 条漏法逐条锁死 + 2 条对照组: 定时 `--broadcast` 仍全量、阶段 1 仍只发微信);
  自测 95 → 108 条全绿。
- 旧用例修正: 4 条老用例把"漏法"当正确行为写死了(其中一条明确断言"非法值应退化为
  全量"), 已按新合同改写 —— 这本身说明旧测试没能拦住这些漏法。

#### A3 · 脏日期可以让整条日报崩, 还能绕过旧闻窗口混进台账(闭环)
- 位置: `scripts/normalize.py` `_parse_date` / `_norm_date` / `validate` / `stale_reason`;
  `scripts/report.py` `policy_date` / `collect` / `policy_row` / `make_policy_detail` / `export_table`;
  `scripts/report_docx.py`; `webapp/app.py` `slim_event`
- 现象(三个叠加的口子, 逐个修掉):
  1. **入库端不校验日期语义**: `_norm_date` 只按**长度**判断, "2026-13-45"(10 位, 但
     13 月 45 日)被原样返回; `validate()` 也不查日历合法性 → 脏日期直接进库。
  2. **脏日期能绕过旧闻窗口**: 窗口用**字符串**比较, "2026-13-45" > "2026-10-12"
     → 被判成"比窗口还新"→ **放行入库**。也就是说, 越脏的日期越容易混进台账。
  3. **日报展示层原样端出脏日期**: `active` 桶会 WARN 跳过, 但 `new` 桶(按 first_seen
     筛)根本不查日期, "2026-13-45" 会原样出现在日报表格、Word 简报与 Excel 的日期列,
     读者无法分辨那是脏数据还是真实日期。
- 修法(源头 → 窗口 → 展示, 三层闭环):
  - **入库端**: 新增 `_parse_date()` 解析真实日历日期(接受 YYYY / YYYY-MM / YYYY-MM-DD
    与 ISO 日期时间前缀); `validate()` 对 `event_date` / `effective_date` 做日历合法性
    校验, 非法即拒收(脏数据不再进库, 也就没有 event_id 漂移风险)。
  - **旧闻窗口**: `_norm_date` 改为语义感知(非法返回 None); `stale_reason` 在
    "日期全部解析不了"时按**日期不明**拒收(fail closed, 技能红线: 日期不明的条目不入库),
    不再放行; `source.publish_date` 写坏时打 `[WARN]` 并退回 event_date 判定。
  - **展示层**: 新增 `report.policy_date()` 统一口径(依次取 effective_date / event_date,
    解析不了就显示"日期异常"), Markdown / Excel / Word / 面板全部改用它; `new` 桶里的
    存量脏日期**保留条目并打 WARN**(不能因为日期坏就把今天的政策从日报里抹掉),
    但日期列显示"日期异常"而不是原始脏串。
- 回归: `scripts/test_smoke.py::DirtyDateGuardTests`(10 条)
- 附: C2(`_norm_date` 对坏值原样返回)随本次一并闭环, 已从"未修复"移出。

#### F0 · `report.collect()` 对非法 `--date` 抛裸 TypeError, 日报与推送一起丢
- 位置: `scripts/report.py` `collect()`; 调用方 `report.py` / `push_report.py` / `report_docx.py`
- 现象: A3 把 `_date()` 改成"解析失败返回 None", 而 `collect()` 拿它做日期减法
  (`_date(date) - datetime.timedelta(days=days_back)`)。**库里非空时**,
  `report.py --date 2026-13-45` 与 `push_report.py --date 2026-13-45` 都抛
  `TypeError: unsupported operand type(s) for -: 'NoneType' and 'datetime.timedelta'`,
  当天日报与推送一起丢, 且是裸 traceback(非中文提示)。空库时因提前 `return None`
  走不到这行, 更难复现 —— 这正是 A3"半修"留下的坑。
- 修法: `collect()` 开头解析并校验 date, 非法即抛 `ValueError`; 三个调用方捕获后打
  中文错误、退出码 2(不再有裸 traceback)。回归: `scripts/test_smoke.py::ReportDateGuardTests`。
- 影响: 非法/手误日期不再静默丢日报, 报错可读, 退出码可被监控区分。

#### F1 · 评审核闸门阶段 2 忘设 `REVIEW_CHANNEL` → 闸门静默失效, 审阅渠道重复收报
- 位置: `scripts/push_report.py` `main()` 阶段选择 + `_mark_pending` / 新增 `_pending_channel`
- 现象: 阶段 2 的"发审阅渠道之外渠道"完全依赖环境变量 `REVIEW_CHANNEL`; `.review-pending/<日期>.flag`
  里明明记了"已发至 企业微信"却从不读回。阶段 2 换个会话/换台机器执行忘了带这个变量时,
  代码静默退化成"全量推送"——**微信(已审阅渠道)重复收到日报**, 钉钉也照收, 日志还显示
  "推送完成(全量)", 看不出闸门没生效。直接违反用户"反馈必须先在原渠道完成再扩散到其它渠道"的红线。
- 修法: `.review-pending` 标记首行写机器可读的 `review_channel=<显示名>`; 阶段 2 以标记里
  记录的渠道为准(环境变量只作兜底), 标记缺失才回退环境变量; 输出显式打印"已排除审阅渠道 X"。
  旧版本写的标记没有该行 → 读回 None → 自动回退, 向后兼容。
- 回归: `scripts/test_smoke.py::PushReportReviewGateTests`
  (`test_stage2_without_review_channel_env_still_excludes_review_channel` /
   `test_stage2_pending_flag_wins_over_mismatched_env` / `test_pending_flag_records_review_channel`)。

#### F2 · `risk.derive_missing()` 遇 `dimension_scores: null` 抛 TypeError, 整批写回中断
- 位置: `scripts/risk.py` `derive_missing`; `scripts/normalize.py` `fill_defaults`
- 现象: `setdefault` **不覆盖显式 null**。Agent 研判输出 `"dimension_scores": null`
  (判"无法评估")或库里记录本身是 null 时, `dims_complete(None)` 抛
  `TypeError: argument of type 'NoneType' is not iterable`; `risk.py --from-file` 批处理时
  一条坏记录就让**整批**中断(前面已写回的保留, 后面的全丢)。
- 修法: `derive_missing` 对非 dict 维度归一为 `{}`; `normalize.fill_defaults` 同样归一;
  `risk.py --from-file` 逐条 try/except, 单条异常只跳过该条、退出码如实反映失败数。
- 回归: `scripts/test_smoke.py::RiskNullDimsTests`

#### F3 · IM 卡片"重点关注"漏掉"高影响"
- 位置: `scripts/push_report.py` `brief_text`
- 现象: 口径只认"中影响 or 收紧", 把合同里最重的一档**高影响**丢进"其他新增"——
  用户第一眼看的微信/钉钉卡片反而把最该关注的政策藏起来了; 且 `report.py` 同一口径用的是
  "高影响 or 收紧", 两处显示层不一致。
- 修法: 统一为"高影响/中影响/收紧"进重点关注, 排序 高影响 > 中影响 > 收紧;
  重点条目附研判依据的条件也补上高影响。
- 回归: `scripts/test_smoke.py::BriefTextFocusTests`

#### F4 · 目录型主机兜底只精确匹配 host, 同站子域漏网
- 位置: `scripts/normalize.py` `_is_aggregate_url` / `AGGREGATE_HOSTS` / `_host_matches`
- 现象: `host in AGGREGATE_HOSTS` 是精确匹配, `mcx.fsvps.gov.ru` 这类同站子域不在名单内,
  同样形态的长描述型目录页可绕过兜底; 顺带发现 `_host_matches` 定义了却**零引用**(死代码)。
- 修法: 改用后缀匹配(`host == s or host.endswith("." + s)`), 死代码 helper 同时启用。

#### F5 · 目录页黑名单里两条 query 规则是死规则
- 位置: `scripts/normalize.py` `LIST_URL_PATH_FRAGMENTS` / 新增 `LIST_URL_QUERY_PARAMS`
- 现象: `"/index.php?list"` 与 `"/?list="` 写在**路径**黑名单里, 但 `_is_aggregate_url`
  先剥 query 再匹配路径 —— 这两条永远匹配不上。于是 `index.php?list=5`、
  `notices?list=all&page=2` 这类目录页能绕过拦截(实测 aggregate=False)。
- 修法: 剥 query 后单独判列表型 query 参数名(`list` / `list[]` / `catid` / `rubric`);
  取名偏紧以免误伤正文页。

#### F6 · `source.official: 1` 逃生口无留痕, 且白占 `verification_notes`
- 位置: `scripts/normalize.py` `check_source_domain`
- 现象: 该分支原来只有一句 `e.setdefault("verification_notes", None)` —— 既没留痕,
  又把本该由核验环节书写的 `verification_notes` 字段占住(写进去还是 None)。
  `official: 1` 意味着"该条 tier=1 不靠官方域名库支撑、只靠抽取环节的显式声明",
  这种记录事后无法与"域名库放行"的记录区分。
- 修法: 自动写 `source.official_note` 审计留痕(注明主机未收录、需人工复核);
  `verification_notes` 保持给核验环节。行为不变(tier=1 仍放行)。

#### F7 · `webapp` 报告列表显示名残留疫情分支前缀
- 位置: `webapp/app.py` `loadReports()`
- 现象: 剥的是 `-daily-report.md`(疫情分支旧命名), 而政策日报落盘名是
  `<日期>-policy-report.md` → replace 落空, 列表里显示成 "2026-10-19-policy-report.md"。
- 修法: 同时剥 `-policy-report.md`。

#### F8 · `webapp/api_summary` 的 `focus_now` 与 `high` 是同一个指标
- 位置: `webapp/app.py` `api_summary()`
- 现象: 两个字段算的都是"高影响"条数, 名字不同值相同, 看的人会以为是两个不同指标。
- 修法: `focus_now` 改按关注档"立即关注"计数(可执行口径), `high` 保持影响等级口径。

#### F9 · `prompts/policy-extraction.md` 的 quote 上限(80 字)与代码(120 字)不一致
- 位置: `prompts/policy-extraction.md` 规则 5
- 现象: 提示词比代码严 40 字, Agent 抽取时切得太短, 浪费可溯源信息(运行时不会报错)。
- 修法: 按 known-issues 建议放宽提示词到 120 字, 与代码/文档一致。

#### F10 · `prompts/policy-impact.md` 未提示"四维齐全时不要手写 score/level/focus"
- 位置: `prompts/policy-impact.md` 等级映射段
- 现象: 提示词只给了加权公式, 没明说"由代码推导"。Agent 手写的 score 与四维加权结果
  相差超过 0.05 分即被 `risk.py` 拒绝写回, 整条研判白跑。
- 修法: 补一条"四维齐全时留空 score/level/focus, 由 risk.py 推导"。

#### F11 · `run_scan.sh` 在 `$HOME` 未设时因 `set -u` 整段退出
- 位置: `scripts/run_scan.sh:19`
- 现象: `[ -f "$HOME/.hermes/.env" ]` 在 HOME 未设置时被 `set -u` 直接打断, 脚本退出,
  cron/systemd 环境下 HOME 可能 unset。
- 修法: 改为 `[ -n "${HOME:-}" ] && [ -f "$HOME/.hermes/.env" ] && . ...` 双层守卫。

#### F12 · `skills/daily-report/SKILL.md` 缺"补发/自定义消息"用法
- 位置: `skills/daily-report/SKILL.md` 步骤 2
- 现象: 新人不知道"日报已发过怎么补发", 也不知道 `run_scan.sh` fallback 走 `--message` 路径。
- 修法: 补 `--force` 补发、`--message` 自定义文本、评审核闸门三段用法。

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
