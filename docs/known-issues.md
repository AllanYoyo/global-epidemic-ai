# 已知问题与限制 (known-issues.md)

> 体检沉淀与工单跟踪。本文件登记代码库**已识别但未修复**的问题,按"高/中/低"分档。
> 修复后从"未修复"移入"已修复"区,保留作为历史与回归参考。
> 上次体检: **2026-10-24** (全量代码审查: 推送链路 / 扫描编排 / 数据合同与展示层三线并行, 高危 H1/H2/H4/H5 当日修复, 见文末 2026-10-24 小节)。

---

## 未修复

### 高 (2026-10-24 体检新增, 建议立即排期)

#### H3 · 取数段非超时失败(rc=1/2)被静默吞掉 —— 空日报照常全量推送, rc=0 假成功
- 位置: `scripts/run_scan.sh` 取数段 FRC 分支(约 :366-372)
- 现象: `FRC` 只在 124/137(超时)时处理, 其它失败码只打一行日志"继续加工出报",
  之后不再被引用。取数段 CLI 以 rc=1/2 失败(ws 提交失败/网关瞬断)而加工段正常时:
  零存档空数据照样出报并 broadcast 推送"今日无新增", 产物校验因无存档目录全部放行
  → cron 记成功; 且该形态不触发孤儿清理, 网关侧孤儿 turn 继续烧配额。
- 建议处理: FRC 非 0 时置 MISSING 或至少记录失败因子, 与 1afdbbd 的"有存档 0 入库
  判失败"对偶补上"取数没跑成也不许判成功"。

#### H6 · 展示/投递层注入三连(数据源自外部网页, 攻击面真实)
- 位置与形态:
  ① **Excel/CSV 公式注入**: `scripts/report.py` export_table —— `title_cn`/`summary_cn`/
     `source.name` 以 `=`/`+`/`-`/`@` 开头时, openpyxl 存为公式、CSV 裸写, 打开台账即
     触发 DDE/外链。修法: 写入前给危险前缀加 `'` 前缀。
  ② **面板 stored XSS**: `webapp/app.py` mdView 用 `marked.parse(t)` 直接赋 innerHTML,
     marked 默认放行内联 HTML; 标题含 `<img onerror=...>` 或 source.url 为
     `javascript://官方域名/...`(入库端只查域名后缀不查 scheme)即执行。修法: 配置
     marked sanitize 或引入 DOMPurify, 链接渲染前校验 scheme 白名单。
  ③ **MEDIA 附件注入**: `scripts/push_report.py` brief_text 把库内标题拼进正文不清洗
     换行, send_hermes 只在尾部追加 `MEDIA:` 行 —— 若 hermes 解析正文中任意 `MEDIA:`
     行[待验证: hermes 的 MEDIA 解析规则], 标题注入即可外发任意白名单路径文件(含
     G11 的凭据快照)。修法: 标题/正文剥离换行 + `MEDIA:` 路径白名单只认
     `data/reports/`(即 G11 处置②, 至今未落地)。

### 中 (建议排期修)

#### M1 · 合并 patch 不过滤空串, 且 source dict 整体替换抹掉 official_note 审计留痕
- 位置: `scripts/normalize.py` upsert merge(约 :656-661)
- 现象: `v not in (None, [], {})` 放过空串 `''` —— 重抽写 `summary_cn: ""` 会覆盖旧摘要;
  `source` 是 dict 整体替换, 旧记录经 `official:1` 逃生口生成的 `source.official_note`
  在下一次合并更新时必然丢失, 事后审计链断裂。
- 建议处理: patch 过滤加空串; source 按 key 深合并或至少保留旧 official_note。

#### M2 · B3 已修复项回归: dry-run 不再提示"今日已推送过"
- 位置: `scripts/push_report.py` dry-run 分支(git 证据: 0459442 重写时删除了 ea9b0e8 加的提示行)
- 现象: 当日已推送后 `--dry-run` 只打"将发送到 X", Agent 会被误导带 `--force` 补发
  造成重复推送(正是 B3 当初要防的场景)。
- 建议处理: 恢复 2026-10-18 B3 的提示行; known-issues"已修复"区该条与现实不符。

#### M3 · HERMES_PUSH_CHANNELS / HERMES_CLI 不在 PUSH_ENV_KEYS —— 人工直跑渠道集合静默不一致
- 位置: `scripts/push_scope.py` PUSH_ENV_KEYS(约 :159-160); `hermes_broadcast_channels`
  运行时读 env, `HERMES_COMMAND` 模块导入时固化
- 现象: 把 `HERMES_PUSH_CHANNELS=weixin` 写进 `~/.hermes/.env`(最自然的配置位置)后,
  cron 经 run_scan.sh source 全量 .env 正常, 人工/Agent 直跑只补 PUSH_ENV_KEYS 那 8 个
  webhook/SMTP 键 → 全量集合静默少发微信(G6 同族缺口)。
- 建议处理: 两个键补进 PUSH_ENV_KEYS; HERMES_COMMAND 改为用时读取。

#### M5 · 全链路无并发锁, am 与 policy 超时预算叠加后可撞车
- 位置: `scripts/run_scan.sh` 全文(无 flock); 单轮最坏 ≈56 分钟
  (预检 180 + 取数 1500 + 孤儿重启 600+180 + 加工 900)
- 现象: am(06:30)拖满即 07:26 结束, 余量稍大就与 policy(07:30)重叠; 手动
  `--stage process` 补跑也可与在跑轮次并发 → 双 Agent 抢配额、collect seq 撞号(C6)、
  范围锁/推送标记竞态。
- 建议处理: `exec 9>"$DATA_DIR/.scan.lock"; flock -n 9 || exit`; crontab.example 补约定。

#### M6 · run_scan.sh 未 export EPIDEMIC_DATA_DIR —— Agent 侧写入目录可能与校验目录分叉[待验证]
- 位置: `scripts/run_scan.sh`(DATA_DIR 就地取值不 export)
- 现象: Agent turn 在网关守护进程环境执行, 不继承 run_scan 的 shell 变量。若
  EPIDEMIC_DATA_DIR 只配在 cron/调用方环境而不在 `~/.hermes/.env`, 产物校验盯
  `$EPIDEMIC_DATA_DIR/reports` 而 Agent 写 repo 内 data/ → G13 的 100% 失败形态复活。
- 建议处理: run_scan.sh `export EPIDEMIC_DATA_DIR`(与 EPIDEMIC_DB 一并), 或校验前
  显式传给下游命令。

#### M8 · 推送幂等无文件锁: TOCTOU 双发 + 部分成功不写 .pushed 导致重发全部渠道
- 位置: `scripts/push_report.py` 幂等检查(约 :550-575)与发送+写标记(约 :706-743)之间无互斥
- 现象: ①并发调用两边都通过 isfile 检查 → 双发; ②广播中任一渠道失败(ok<len)→ 不写
  `.pushed` 也不写 pending → 下一次合法调用把已成功渠道再发一遍。
- 建议处理: 写标记改为"先占后发"(O_EXCL 创建 + 内容记录已发渠道), 失败重试只补发缺口。

#### M9 · 外部提供的 event_id 不校验 12 位 hex, 一条脏记录打崩整个面板
- 位置: `scripts/normalize.py` fill_defaults(约 :621, `e.get("event_id") or ...`) +
  `webapp/app.py` `_jitter`(`int(event_id, 16)` 无保护)
- 现象: 输入 JSON 手写 `event_id: "ZZ-9"` 原样入库; 该国地理编码命中时 `/api/events`
  抛 ValueError → 整体 500, 台账+地图全空, 无单条隔离。
- 建议处理: normalize 校验外部 event_id 为 12 位 hex; webapp _jitter 捕获异常回退 0 偏移。

#### M10 · policy_key 在 event_id_of 不做 strip/lower, 空白/大小写差异导致同一政策拆条
- 位置: `scripts/normalize.py`(约 :434 vs :445 —— `_subject_components` 有 strip,
  `event_id_of` 的 `e.get("policy_key")` 直接用)
- 现象: `' AB '` 与 `'AB'` 算出不同 event_id, 同一条政策 first_seen/研判结论分家(B2 同族)。
- 建议处理: event_id_of 对 policy_key 同样 strip().lower()。

#### M11 · 无斜杠 URL(`https://host?list=all`)整体绕过聚合页拦截
- 位置: `scripts/normalize.py` `_is_aggregate_url`(约 :316-322, 早退发生在 query 判定
  与 host 兜底之前)
- 现象: URL 在 host 后直接跟 query、无 `/` 时, 连 AGGREGATE_HOSTS 兜底都被跳过
  —— 2026-10-19 专门堵的目录页绕道口子在这类写法下重新打开(F5 的姊妹)。
- 建议处理: 早退前先完成 query 参数与 host 兜底判定。

#### M12 · geo 编码持全局锁做网络请求, 冷缓存时面板分钟级无响应(C12 叠加无自愈)
- 位置: `webapp/app.py` country_centroid(锁内 sleep 限速 + urlopen 8s 超时)
- 现象: 首次部署/缓存冷时一次请求串行查 N 国(N×1.1s+), 期间所有需要 geo 的请求排队;
  叠加 C12(失败永久缓存 None)后该国会从地图永久消失。
- 建议处理: 锁只护 cache 读写, 网络请求移出锁外; 失败不落缓存(与 C12 一并修)。

#### M13 · collect.py 抓 PDF 按 utf-8 强解成 mojibake, 且 resp.read() 无大小上限
- 位置: `scripts/collect.py` fetch(约 :43-51)
- 现象: ePing PDF 直链是流程明文要求存档的对象, `application/pdf` 无 charset →
  errors="replace" 解码后存档不可读, "source.quote 逐字摘自存档文件"对此类存档不可满足;
  超大响应全量入内存; 30s timeout 是 per-blocking-op, 慢滴流可拖更久。
- 建议处理: 二进制 Content-Type 直接落盘不解码; 流式读设上限(如 5MB)。

### 中 (建议排期修) · 历史遗留

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

### 2026-10-25 午后 · 轮转档 23 个政策源无任何调度日(每日全扫决策)

#### H17 · weekly_shards 配置断裂: border_neighbours/export_partners 两档永远扫不到
- 位置: `scripts/scan_targets.py`(ALWAYS_TIERS/shard 过滤)、`config/sources.yaml`(weekly_shards)
- 实证(本地逐日渲染 10-20~10-26): policy 源 47 个, 一周不重复并集仅 24 个 —— 
  23 个源(俄罗斯×2/越南/哈萨克/蒙古/日本/韩国/朝鲜/老挝/缅甸/土耳其/波兰/塞尔维亚/
  乌克兰/罗马尼亚/挪威/阿塞拜疆 + 澳/新西兰/乌拉圭/沙特/墨西哥/英国)没有任何调度日。
  根因链: 10-14 `029941f`+`2beb2e5` 建立五档轮转 + 条目级 shard(commit 明言
  "逐日 31-33 源, 7 天全覆盖零孤儿"); 10-24 `92dbe1e` 新建 scan_targets.py 时
  ALWAYS_TIERS 只列 3 个必扫档, 而 weekly_shards 周一~五/周日 scan 全空 ——
  两个轮转档没有任何一天被调度, 条目 shard 永远轮不到检查, 与 AGENTS.md
  "7 天全覆盖"的文档合同直接冲突。失明恰含 10-14 特意标注"高疫区邻国显式
  每日扫"的俄/越/哈/蒙。失明窗口: 10-24 确定性管线上线起 2 天(此前 Agent
  时代自行读 sources.yaml, 配置上覆盖)。
- 修法(当日闭环, 依用户决策"每日全扫"): P4 确定性取数(curl, 零 LLM)后源数
  不再消耗 token —— 抽取成本只跟存档数走(每日 ≤12 硬上限), 周轮转失去存在
  前提。①ALWAYS_TIERS 扩为六档全列; ②shard 过滤删除(字段保留解析兼容旧
  YAML); ③weekly_shards scan 全部清空(段保留兼容解析器); ④_pick 按源 id
  去重(同源跨档/跨国重复挂载只扫一次); ⑤tier_today 去重防周六重复档。
- 验证: 自测 218 全绿, 含新不变量(任意两天清单完全一致/六档全在/轮转档源
  ru-fsvps·uk-defra·jp-maff 等在场/同源不重复/7 天并集=每日清单);
  渲染 47 源对 → 8 块(6×7+5)。
- 取舍登记: 47 源并行 curl(8 并发, 单页 15s)预计 2-6 分钟, 在 10 分钟约束内;
  当日 12 存档上限下高优先档先扫先占坑(picked 固定优先级序), 触顶时低优先
  档候选次日还有机会。若常态化触顶, 再议调上限或按档配额。

### 2026-10-25 晨 · 新链路 cron 首跑失败(囤积不落盘 + 加工段抢跑)

#### H16 · 249.6 万折合换 0 入库: 取数 Agent 囤积候选不落盘, 加工 Agent 空输入下自发联网重做取数
- 位置: `scripts/run_scan.sh`(分块循环/空输入短接/prompt 纪律)、`scripts/scan_targets.py`
  (GROUP_CHUNK 分块)、`scripts/probe_channels.py`(--group 放开)
- 实证(scan.log + state.db, 北京时间):
  - 07:30:12 组1(必扫 21 源对, 上限 1200s)启动; 取数会话 239f3a86 **api 50/50 打满**,
    84 次 terminal/131KB、web_search 2 次, 最终产出是**纯文本候选清单**(13 条带 URL 日期
    + 9 个死通道 + 「收尾建议(未执行)」)——0 存档、0 个交付文件(search-log/candidates/
    extracted 全没写), 07:40:33 rc=2 结束。病根: 「每找到一条立刻三步」纪律未约束住模型,
    Agent 选择先全部浏览、收尾统一存档, 轮次耗尽在收尾之前; 且 21 源对 × ~4-5 轮/源
    本身就装不进 50 轮(10-24 晚 --only-group 2 实测 21 轮/4 源对)。
  - 07:40:35 加工会话 e549106a 启动: 输入清单全空, prompt 指令「直接跳到第 4 步出报」被
    无视, Agent **自发联网重做取数**(违反「不联网检索」红线), 07:56-07:58 落了 6 份真实
    存档(泰国 DLD 第 247 号令/WTO 秘鲁/哥伦比亚/乌克兰通报/巴西 MAPA ×2), 自己也
    **50/50 打满**(568k 输入/401k 输出/9.7M cache 读 ≈194 万折合), 入库/出报没跑到;
    08:10 CLI 超时被杀后孤儿 turn 继续跑到 08:29(网关 restart deferred 等活跃 turn)。
  - 终检正确判失败(缺日报/推送/入库 0), 广播锁当场撤销; 全天折合 249.6 万、无 429、
    无 90 轮狂奔、单条工具输出受控(execute_code 均 2.7KB)——token 护栏全部生效,
    输的是**功能**, 不是消耗。
- 修法(6345ac5):
  · **分块化(治囤积+不收敛)**: 当日全部源对按 `GROUP_CHUNK=6` 切成 K 块(周日 21 对 →
    6/6/6/3 四块), run_scan.sh 逐块发起(变长 `n1=.. nK=..` 解析/超时按源对加权/块间预算
    复查), 单块打满只废单块; 取数纪律加**进度自检**「扫到第 3 个通道时 data/raw 下必须
    已有存档, 否则你在囤积候选」+ 点名 10-25 教训。
  · **空输入短接(治抢跑)**: 0 存档且无候选/抽取记录时加工段**不派 Agent** —— run_scan.sh
    直接 `report.py --excel --docx` 确定性出报、按范围锁确定性推送(broadcast/review 才推)、
    自动补说明性 policy-findings.md; 加工段红线加「严禁联网检索与 collect.py 抓取新原文,
    日报 0 新增是诚实结果」。
  · 附带: 产物计数统一(jsonl 缺失时 wc 不再抛 bash 报错行); --only-group 放开为任意块号。
- 验证: 自测 211 → **212 全绿**(分块不变量: 块号连续/每块 ≤6/各块和=全量/块K=全量第 K 段;
  空输入短接静态断言; 进度自检进 prompt); VPS 干跑: `--groups` 输出 n1=6 n2=6 n3=6 n4=3,
  空输入全流程 hermes 调用恰 4 次(仅取数块, **零加工调用**), 确定性出报 rc=0 产物校验通过。
- 遗留观察: ①6 对/块是否够收敛要看下一个工作日 cron 实测(必要时 GROUP_CHUNK 降到 4-5);
  ②网关 restart 对活跃 turn 的 deferred 等待(本轮 ~20 分钟)仍是已知慢点, 未动。

### 2026-10-24 晚 · 一天 6 会话烧穿 Token Plan(重复取数 + 不收敛 + 输出纪律失守)

#### H15 · 当日 Token Plan 打穿(429): 全部存档 09:39 已落齐, 之后三轮重取零新增
- 位置: `scripts/run_scan.sh`(幂等锁/预算刹车/拆组循环)、新增 `scripts/token_budget.py`
  / `scripts/probe_channels.py` / `scripts/jsonl_append.py` / `scripts/snip.py`、
  `scripts/scan_targets.py`(--group/--groups)、`scripts/push_scope.py`(锁时效 60→75)
- 实证(state.db 会话明细 + scan.log + messages 表 chars 下钻, 均为北京时间):
  | 时间 | 会话 | 发起方 | api 轮 | 输入 | cache 读 | 结果 |
  |---|---|---|---|---|---|---|
  | 07:30 | 8192ea35 | cron(撞 PROMPT_FETCH 空串缺陷跑成单轮) | 70 | 411k | 17.3M | 出报+推送 |
  | 09:27 | 9e2a31ae | 手动 run_scan.sh | **90(打满)** | 374k | 16.4M | **09:39 落齐全部 6 条存档**后超时 |
  | 12:50 | 3d45b052 | 网关侧 ad-hoc(scan.log 无记录, **旧 prompt**) | **90(打满)** | **614k** | **19.4M** | read_file 12 次 125KB(含 58KB sources.yaml); 零新增 |
  | 13:54 | 569361b6 | 网关侧 ad-hoc(新 prompt, **无超时看护裸跑 48 分钟**) | **50(打满)** | 226k | 5.0M | 零新增 |
  | 14:55 | 39303181 | ad-hoc + 委派 | **50(打满)** | 182k | 3.6M | 零新增 |
  | 14:57 | aad0682d | 上轮 delegate_task 派生 | 36 | 234k | 4.4M | 完成推送 |
  当日合计 ≈204 万输入 + 130 万输出 + 6600 万 cache 读 / 386 次 API, 产出 = 6 存档 4 入库
  (存档 mtime 证明全部来自 09:27 那轮); 16:21 起 errors.log 连续 429「Token Plan 用量
  上限」, 连微信对话都调不动。三个病根: ①**重复扫描无闸** —— hermes -z 是开放入口,
  任何会话可拼扫描 prompt 发射, 绕开超时/幂等/日志, 而"今日取数是否已完成"无任何锁;
  ②**取数段不收敛** —— 每轮都撞 max_turns(90/90 ×2、50/50 ×2), 打满即整轮作废重来,
  叠加"每轮全量重发上下文"的计费结构, 单轮成本 ≈ 上下文 × 轮数; ③**输出上限靠
  prompt 约束不住** —— 治理后单次 execute_code 仍 13.4KB(红线 8KB), 25 次累计 115KB。
- 修法(三提交四件套, 依「确定性优先」路线):
  · **e84f277 幂等锁+预算刹车**: `data/raw/<日期>/.fetch-done`(取数交付即写, 后续
    任何取数请求见标记直接跳过进加工, 强制重取显式 `--refetch`; 超时且 0 存档不写、
    允许重取); `token_budget.py` 只读 state.db 统计东八区当日折合消耗
    (input+output+cache_read×0.1), ≥400 万(可覆盖)即 rc3 → run_scan.sh 降级为只加工;
    `--stage fetch` 统一发射口(自带看护, scope 强制 none)。
  · **560ae37 拆组+预探**: 取数按档位拆两组逐个发起(必扫档/轮转档, 超时按源对数
    加权、保底 360s, 单组打满只废单组); `probe_channels.py` 用 curl 8 并发预探当日
    全部源入口, [dead] 清单注入 prompt(Agent 对死通道零工具调用); 分支条件查
    `PROMPT_FETCH_TPL` 的静默退化 bug 由干跑抓到并修; `SCOPE_MAX_AGE_MINUTES`
    60→75 防合法锁被误判过期。
  · **f9ca490 输出硬上限**: `jsonl_append.py`(追加只回执一行)/`snip.py`(摘录硬性
    ≤2KB)把纪律变成脚本保证, prompt 内联抽取键值模板(补上「禁读文件却让按
    policy-extraction.md 抽」的缺口); 部署时 VPS `tool_output.max_bytes`
    50000→12000(全局兜底)。
- 验证: VPS 真机干跑 5 场景全过 —— 预算刹车(真实 state.db 当日 998 万折合 → 拦截
  +search-log 留痕 +fetch-only 判失败 rc1)/取数写标记/幂等跳过(prompt 不再生成)/
  --refetch 重取/分组双轮(组1 21 源对 1008s + 组2 4 源对 360s, 预探实探 25 源,
  ca-cfia 连接失败/usda-aphis 403 与 search-log 历史记录一致); 两组 prompt 零 ASCII
  双引号/反引号/$; jsonl_append/snip 冒烟通过(冒烟抓到 title_cn/source.url 误拒
  bug 一处, 已修)。自测 193 → **208 全绿**(TokenBudgetBrake 6 + FetchGrouping 6 +
  ToolOutputCap 4, 另更新 TokenEconomy 1 条断言随行为)。
- 二级小token实测(21:52, --only-group 2 州级档, 4 源对): 会话 12667b2f
  **api=21 轮 / 折合 26.4 万**(对照出事日同类取数 50-90 轮 / 170-300 万),
  最大单条工具输出 11.3KB ≤ 网关新上限 12KB —— 经济学验收达标; 轮转档当天
  无新政策、0 新增存档属正常。测试同时抓到并当日修复一个真 bug(1053529):
  **调用方传的 STAGE_FETCH_TIMEOUT 会被 ~/.hermes/.env 的值覆盖**(与 10-22
  HERMES_RUN_CMD 同类), 实测 600s 被顶成 1200s 跑满 20 分钟; 阶段超时已进
  「调用方优先于 .env」保护名单, 修复后假 .env 复验通过。另: 当日 18:01 的
  误启 am 扫描(f77ade00, 无参防护修复 4c74fcc 的起因)在 Token Plan 恢复后
  实际计费 **折合 ~167 万**(46 万输入 + 34 万输出 + 875 万 cache 读, 跑满
  50 轮才被网关重启打断) —— 此前「损耗为零」的判断有误, 以 state.db 为准。
- 预期: 重复取数被幂等锁挡死; 单轮不收敛的爆炸半径从整轮缩到单组; 死通道/探测
  从每轮 20-40 次工具调用归零; 输出放大被硬上限压住。次日 cron 实测校准。

### 2026-10-24 下午 · 取数段 token 二次治理(复杂度反噬)

#### H14 · 取数段单轮消耗 65k→200k: 治超时的复杂度本身成了预算杀手
- 位置: `scripts/run_scan.sh`(prompt 重写)、新增 `scripts/scan_targets.py`、
  `scripts/normalize.py`(_load_input)、`scripts/synth_candidates.py`(JSONL 化)
- 实证(state.db 逐日曲线): f7ae1c4 前单轮流程 ~65k/轮; 10-22 拆段+10-23 三连修后
  实测 200-250k/轮(10-24 取数段 60 轮 10.1M 输入, 60 轮里把当日 Token Plan 打掉 75%)。
  同口径工具构成对照(旧 10-13 会话 39 轮 vs 10-24 取数段 60 轮):
  ① 代码/终端调用 21 次(平均 461 字符)→87 次(平均 2.6KB) —— 「边存边写」把每条
  候选从 1 次调用变成 5-9 次(collect+grep+python 读改写+打印验证);
  ② read_file 4 次→11 次, 最大单次 58KB(Agent 临场读 AGENTS.md/SKILL.md —— prompt
  第一步就叫它读 sources.yaml/SKILL.md); ③ web_search 10 次→≈0(三连修的成果,
  方向正确)。结论: **差距不在检索量, 在流程复杂度反噬** —— 为治理预算引入的
  纪律让每条候选成本翻了 3 倍。
- 修法(四件套):
  · **当日清单预渲染注入**: 新增 `scan_targets.py`, 跑批前把当天相关国别/源入口
    (必扫档+周轮转档)确定性渲染成 ~30 行注入 prompt, 取数段**严禁 read_file/cat
    任何仓库文件**; 渲染失败退回旧指令不阻塞。
  · **写盘 JSONL 化**: candidates/extracted-policy 改 .jsonl 逐行追加(open 'a' 写
    一行、**不打印任何内容**、写完不回读不验证) —— 消灭"python 读改写整个 JSON,
    每追加一条读回一次"的上下文放大; normalize._load_input 支持 .jsonl(坏行跳过
    计数); synth_candidates 兼容读旧数组版与 jsonl、输出 jsonl。
  · **prompt 瘦身**: 取数/加工段 prompt 重写, 删理由解释与重复强调, 纪律条目化;
    单次工具输出红线从 30KB 收紧到 8KB。
  · **轮次上限(部署侧)**: Hermes config.yaml 顶层 max_turns 90→50(取数段正常
    30-40 轮, 上限兜底; 不改 repo, VPS 上调整)。
- 验证: 干跑桩掉 HERMES_RUN_CMD 落盘 prompt, 断言清单真实注入(ca-cfia 等源 URL
  直出)/关键指令齐全/无断串; 自测 188 → **193 条全绿**(TokenEconomyTests 5 条)。
- 预期: 取数段单轮回到 ~65-90k, 一轮总输入 10M → 3-4M(待下一轮 cron 实测校准)。

### 2026-10-24 全量体检 (高危四条当日闭环)

#### H1 · EXIT trap 撤锁机制被初始化语句的位置错误整体杀死
- 位置: `scripts/run_scan.sh`(8a3ac17 引入; 修复: 布防初始化移到范围声明 case 之前)
- 现象: `_BROADCAST_ARMED=0` 写在 case 块**之后**无条件执行, 把 broadcast 分支刚设的
  1 立刻清零 —— `trap _revoke_broadcast_lock EXIT` 在**任何**退出路径上都空转,
  "任何非正常退出一律撤销广播锁"的保护(183 行注释自宣)形同虚设。网关预检失败
  exit 1 / SIGTERM / OOM 场景下 broadcast 锁残留盘上 ≤60 分钟, 窗口内
  `push_report.py --broadcast` 被放行 → 群发钉钉(10-20 事故重演路径)。
- 修法: 初始化移到 case 之前(set -u 下 trap 函数需要它已定义); 成功路径 476 行的
  disarm 保留。行为级验证: 新顺序 trap 输出"撤锁生效", 旧顺序输出"空转(锁残留)"。
- 回归: `test_smoke.py::ScanFailureGuardTests::test_run_scan_trap_armed_before_scope_case`
  (静态断言初始化 < case < 置 1 的行序)。

#### H2 · 产物校验把交付文件当"存档"计数 —— 安静的一天必判失败, 满额日误判超限
- 位置: `scripts/run_scan.sh` RAW_N(419-437)
- 现象: 原先"数所有非 meta 文件"把取数段 prompt **强制**创建的交付文件
  (candidates.json / extracted-policy.json / search-log.md, 要求"哪怕 0 条也要建空
  数组")与加工段产物(policy-findings.md)全数进去 —— 安静的一天 RAW_N 也 ≥3,
  「当日有存档却 0 入库」恒触发 → policy 档每天必红, "安静的一天允许成功"分支
  永不可达; 满额日(6 html + 6 meta + 交付文件)还撞 `>12` 误判"穷举下载"。
- 修法: RAW_N 改为只数 `*.meta.json`(collect.py 每条存档恰写一个), 计数即真实存档
  条数, 交付/中间文件天然排除。验证: 2 条存档+6 个交付文件场景, 新计数=2, 旧计数=7。
- 回归: `test_smoke.py::ScanFailureGuardTests::test_run_scan_counts_meta_files_only`。

#### H4 · DEFAULT_SENTINELS 缺 action_type/policy_domain —— 合并更新时兜底值洗掉真实结论
- 位置: `scripts/normalize.py`(修复: 哨兵表补两键)
- 现象: `fill_defaults` 给缺省的 `action_type` 填 `"调整"`、`policy_domain` 填
  `"measures"`, 但合并保护表 DEFAULT_SENTINELS 只有 verification_status/policy_status
  两键 —— 同一显式 event_id(镜像回放/增量更新路径)二轮重抽没识别出动作/领域时,
  兜底值照常进 patch 把库里的 `收紧`/`animal` 洗掉, 直接违反"空值和兜底默认值
  不覆盖已有核验/研判结论"的合同(单测复现)。
- 修法: DEFAULT_SENTINELS 补 `action_type: "调整"`、`policy_domain: "measures"`,
  与 fill_defaults 的兜底保持一致。
- 回归: `test_smoke.py::NormalizeMergeTests::test_fill_default_sentinels_do_not_overwrite_real_conclusions`。

#### H5 · 阶段 2 的"无待审记录即拒绝"可被 REVIEW_CHANNEL/范围锁兜底绕过 —— 未经确认即群发
- 位置: `scripts/push_report.py`(修复: 阶段 2 只认 .review-pending 待审标记)
- 现象: `review_label = pending_label or lock_label or env_label` 的兜底链让"无待审
  记录即拒绝"只在三者全空时生效。而 `run_scan.sh --review-channel` 路径既
  `export REVIEW_CHANNEL` 又写 review 锁 —— Agent 跳过阶段 1 直接 `--review-approved`
  就会向"除审阅渠道外全部渠道"群发(钉钉照收), 违反"先在原渠道确认再扩散"红线;
  `--review-approved --review-channel` 显式组合同样无检查。
- 修法: ①阶段 2 的 review_label **只认待审标记**(它是"审阅版已发出"的唯一凭证,
  范围锁/env 只能证明"声明过范围"); ②`--review-approved` 与 `--message` 互斥
  (自定义文本没有可批准的待审对象); 拒绝文案同步改口径。
- 回归: `test_smoke.py::PushReportReviewGateTests::test_stage2_with_env_or_lock_but_no_pending_is_refused`
  (env/锁/显式参数三场景)与 `::test_message_with_review_approved_is_mutually_exclusive`;
  同步修正既有断言 `test_stage2_without_any_review_channel_is_refused` 的文案。
- 自测: 183 → 188 条全绿。

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
- 回归: 自测 143 → 151 条全绿(新增 `ReportAttachmentTests` 8 条: 幂等/补生成/补不上/
  推送前补齐/正文如实标注/页脚标注/渠道侧告警/SystemExit 降级)。

#### G8 · 诊断脚本把"历史日志里没有 scope= 标记"一律报成"生产在跑旧版" → 误报
- 位置: `scripts/diagnose_push.py`(`check_cron_log` 的 scope= 判据 + 新增
  `_current_run_scan_has_scope`)
- 现象(2026-10-20 部署后跑生产自检): 报「[警告] scan.log 里没有 scope= 标记
  —— 说明这次跑的是 2026-10-20 之前的旧版 run_scan.sh」。**误报** —— 判据只看了
  累积的历史日志, 而最近那次跑批(10-20 07:30)本来就发生在新 run_scan.sh 部署
  **之前**, 缺 scope= 是预期内的; 生产代码早已是新版、闸门是生效的。误报会让
  每次部署自检都"发现 1 个问题", 反而让人以为闸门没上线。
- 修法: 判据改成看**当前代码**(`scripts/run_scan.sh` 里是否含 `scope=`)——
  当前代码有 → 打印说明("最近那次跑批早于本次部署, 下一次跑批起就会有"), 不报警;
  当前代码也没有 → 照旧报警。**不比对日志时间戳**: 日志时间戳是东八区
  (`run_scan.sh` 里 `export TZ`), 文件 mtime 是服务器本地时区(UTC), 直接比会差 8 小时。
- 回归: 自测 151 → 153 条全绿(新增"当前代码有 scope= 不误报"与"当前代码没有
  scope= 仍报警"两条)。

### 2026-10-22 检索失败事故 (G9)

#### G9 · 跑批"假成功" + 残留广播锁放行全量推送 + 取数无降级顺序
- 位置: `scripts/run_scan.sh`(看门狗 / 产物校验 / 网关预检 / 失败即撤销声明);
  `scripts/push_scope.py`(`SCOPE_MAX_AGE_MINUTES` / `scope_age_minutes` / `scope_is_stale`);
  `scripts/push_report.py`(`--broadcast` 过期校验); `scripts/report.py`(`ensure_docx` 语义);
  `skills/global-policy-search/SKILL.md`(取数工具纪律)
- **现场事实(10-22 07:30 定时跑批)**: 检索阶段卡死 44 分钟, Agent 自行放弃, 但
  **rc=0** —— 一条没入库、一份日报没出(raw 目录都没有), 监控完全看不出来。
  日志只有一行 `I stopped retrying because I kept running web_search 50 times`。
  网关日志同时段: `CDP supervisor ... connect failed (attempt 1..5/5): Connect call
  failed ('127.0.0.1', 37149)` ×4 组, 随后 `Blocked web_search: this turn has
  already made 50 web searches ... runaway search loop`。
- **四个代码缺陷(缺一不可)**:
  | # | 缺陷 | 后果 |
  |---|---|---|
  | ① | `run_scan.sh` 直接透传 Agent 退出码, **不校验产物** | Agent 放弃也是 rc=0 → cron 记成功 → **故障静默一整天** |
  | ② | cron 早上写下的 broadcast 锁**没有有效期** | 那一轮没推成, 锁留在盘上 → 当天任何时候 `push_report.py --broadcast` 都被放行 → 群发钉钉(**10-20 事故重演**, 且 cron 上下文校验拦不住: 锁是 cron 写的) |
  | ③ | 取数**没有降级顺序与搜索预算**(skill 未约束) | CDP 连不上就死磕同一工具, 50 次打转到撞上限, 白烧 44 分钟 |
  | ④ | `--ensure-docx` 在**整份日报都没生成**时也补 Word | 补出 62KB **空壳日报**, 看着像"报告已出", 实际当天 0 条记录 —— 比缺文件更坏 |
- **修法**:
  - `run_scan.sh`: 跑批加 `timeout` 硬上限(默认 2400s); 跑完**校验产物**
    (日报 md + 按范围锁决定的推送/待审标记, 且必须是"这一轮新生成"的, 用 mtime 判定);
    校验不过 → `[失败]` + 退出码 1(不再假成功); 校验通过才补 Word。
    另加**网关健康预检**(`hermes status`, 最多等 180s): 10-20 实测撞上网关重启
    (`Gateway draining`)时整轮扫描 8 秒就废了。
  - **失败即撤销声明**: 那一轮没推成 → `run_scan.sh` 当场 `push_scope.py --clear`。
  - `push_scope.py`: 广播锁加 `SCOPE_MAX_AGE_MINUTES=60`(跑批硬上限 40 分钟 + 推送余量);
    `scope_is_stale()` 判定过期; 解析不出 `declared_at` 的旧锁不判过期(不误伤历史锁)。
  - `push_report.py`: `--broadcast` 遇过期锁 → 拒绝并提示重新声明(写明依据)。
  - `report.py`: `ensure_docx` 改为**同源 md 缺失就不补**(补出来是空壳, 掩盖失败)。
  - `skills/global-policy-search/SKILL.md`: 新增「取数工具纪律」——
    优先级 `collect.py` 直抓 > `web_fetch` > 浏览器(**sources.yaml 里已���官方入口 URL,
    不要用搜索去找官网**); 同一工具失败 2 次就换; 单轮 `web_search` ≤ 15 次;
    浏览器失败就放弃浏览器; 宁可少收也不要卡死。
- **回归**: 自测 153 → 162 条全绿(新增 `ScanFailureGuardTests` 9 条: 过期锁拒绝/
  窗口内锁放行/新鲜锁放行/锁龄计算/产物校验/看门狗/网关预检/skill 纪律/空壳不补)。

#### G10 · 环境层: 网关双服务冲突 + 浏览器 CDP 端口残留(非本仓库代码, 但影响跑批)
- **现场事实**: 系统级 `hermes-gateway.service` 在跑(自 10-19 23:30 UTC 起), 同时
  **用户级** `~/.config/systemd/user/hermes-gateway.service` 处于 `activating (auto-restart)`
  崩溃重启循环(`status=75` = 锁 `/root/.hermes/gateway.lock` 被系统级那个占着)。
  后果: 每隔几秒空转一次(`CPU: 3.730s`/次), 并让"网关是否就绪"难以判断。
- **CDP 连不上的真因**: 网关里的 browser supervisor 记着一个**已死的 CDP 端口**
  (37149) —— 该端口上没有任何进程监听(`ss -ltnp` 只有网关 38929 与手工起的
  cdptest chrome 39999)。 supervisor 反复重连 5 次失败后放弃。
  另有 4 个 14 天前遗留的孤儿 `browser_harness.daemon`(ppid=1, RSS 0–76KB)。
- **内存真相(与"6 个浏览器守护进程吃掉 1975MB"的说法不同)**: 真正的内存大头是
  **网关主进程本身**(RSS 601MB, systemd 口径 923MB, 已连续运行 2 天);
  那 6 个 `browser_harness` 里 4 个是 RSS≈0 的空壳, 2 个新的各 40–45MB。
  杀掉它们最多释放约 85MB, **且不会修好 CDP 端口残留**。
- **处置**: 禁用用户级服务(只保留系统级那个); 重启系统级网关 —— 一次同时解决
  ①释放 601MB ②清掉陈旧 CDP 端口记录 ③清掉卡死的 44 分钟会话与 50 次搜索计数。

#### G11 · Hermes 快照脚本把全部环境变量明文落盘(含凭据), 且落在媒体投递白名单目录里
- 位置: `/root/.hermes/cache/scratch/hermes-snap-*.sh`(Hermes 自身产生, **非本仓库代码**)
- **现场事实(10-22 只读核查, 全程未读取任何值)**:
  - 快照脚本用 `declare -x` 把 **110 个环境变量逐条明文写盘**; 其中至少 8 个是凭据类:
    `DINGTALK_SECRET` / `DINGTALK_WEBHOOK`、`HERMES_CUSTOM_CHAT_INTERN_AI_ORG_CN_API_KEY` /
    `HERMES_CUSTOM_DEVELOPER_AMD_COM_CN_API_KEY` / `HERMES_CUSTOM_LLM_LONGAI_VIP_API_KEY`、
    `X_ADMIN_TOKEN`、`TERMINAL_DOCKER_SHARED_CONTAINER_KEY`、`HERMES_REDACT_SECRET`。
  - 文件权限 `600 root:root`、目录 `700 root:root` → **本机其他用户读不到**(不是裸奔)。
  - **但 `HERMES_MEDIA_ALLOW_DIRS` 包含 `/root`**, 而 Hermes 以 root 运行 →
    该目录整体落在**媒体附件投递白名单**内。本项目推日报时用 `MEDIA:<path>` 送 Word,
    若哪一轮把 `hermes-snap-*.sh` 当成附件发出去(例如路径写错、或把 scratch 目录
    整目录作为附件), **凭据即随日报外发到微信/钉钉/邮箱**。
  - 危害路径是"投递"而不是"本机读取", 所以收紧文件权限**挡不住**这条路径。
- **处置(待执行, 需谨慎)**: ①Hermes 侧改为**只导出白名单变量**(非凭据类),
  凭据类不落盘 —— 这是根治; ②在 `push_report.py` 的附件选择上**只允许日报自身目录**
  (`data/reports/`) 下的文件, 不接受任意 `MEDIA:` 路径; ③把 `~/.hermes/cache/scratch`
  从 `HERMES_MEDIA_ALLOW_DIRS` 剔除; ④现存快照文件**轮转即清**(旧的不删也行,
  但不应有新的)。
- **注意**: 本条涉及真实凭据, **任何处置都不得把密钥值写进代码、提交、文档或回复**;
  报告只写"哪些键名被落盘"。

#### G12 · ePing 行式列表的真实行形状与文档不一致, 按下标解析会全军覆没
- 位置: `scripts/parse_eping.py`(`_from_text`); `skills/global-policy-search/SKILL.md`
- **现场事实(10-22 生产实机首跑)**: 10-21 成功轮的产物 `sps_list.txt`(26 行)真实形状是
  `序号 | 成员 | 日期 | 文号+标题 | 直链`, 而 skill 文档写的是
  `成员 | 日期 | 文号 | 标题 | 直链`。两处差异:
  ①行首有**序号列**, 成员在第 2 段; ②**文号与标题合并在一段**(空格分隔),
  没有独立标题列; ③日期段可能**有两个**日期 —— 第一个是发布日(dist), 第二个通常是生效日。
- **后果**: 按固定下标解析的真实结果是「窗口内 0 条 / 26 条无发布日期」= **全军覆没**;
  而"解析不出日期"的行若被误当成"无日期"就会在后续流程里引发逐条下载核日期 ——
  正是 10-22 卡死 40 分钟的路径。
- **修法**: 行式解析改为**按内容判定**(文号正则 / 日期正则 / URL / 序号数字),
  **不依赖下标**; 行式产物形状会随 Agent 的解析写法漂移, 固定下标一漂移就全废。
  动作判定同时看 kw 与**标题**(行式没有关键词列, kw 为空)。
- **回归**: 自测新增真实形状 2 条(序号列不能被当成员 / 双日期都要留住);
  全量 179 → **181 条全绿**。生产实机复验: 26 条全部正确解析, 收敛出 17 条候选,
  **零 PDF 下载**。

#### G13 · `run_scan.sh` 产物校验路径写死 `repo/data`, 与其余脚本的 `EPIDEMIC_DATA_DIR` 不同源
- 位置: `scripts/run_scan.sh`(`REPORT_MD` / `PUSH_FLAG` / `PENDING_FLAG` / `RAW_N` 四处)
- **不一致的证据(10-22 审计实测)**: 全仓库只有 `collect.py` 与 `normalize.py` 读
  `EPIDEMIC_DATA_DIR` / `EPIDEMIC_DB`; `report.py` / `report_docx.py` / `push_report.py` /
  `push_scope.py` / `diagnose_push.py` 都 `from normalize import DATA_DIR`(同源, 没问题);
  **只有 `run_scan.sh` 的产物校验绕开了它, 直接拼 `$REPO/data/...`**。
- **后果**: `README.md:129-130` 明确教生产设 `EPIDEMIC_DATA_DIR=/opt/global-epidemic-ai/data`。
  一旦照做, Agent 的日报落在 `$EPIDEMIC_DATA_DIR/reports/`, 而校验盯的是
  `$REPO/data/reports/` → 跑批**每一轮**都判 `缺少: 日报Markdown` 并退出 1, 即 100% 失败,
  且失败信息指向错误原因。生产现在**只是因为恰好没设这两个变量**(数据落在 repo 内)才没炸。
- **现场佐证**: 生产 `~/.hermes/.env` 里没有 `EPIDEMIC_*`, 而 repo 外
  `/opt/global-epidemic-ai/database/epidemic.db`(24KB, 0 行, 连 `first_seen` 列都没有)
  与 `/opt/global-epidemic-ai/data/raw|reports`(停在 09 月)就是**当年设过、后来被撤**留下的孤儿数据。
  两个路径都存在且内容分叉, 正是这个不一致的历史后果。
- **修法**: `run_scan.sh` 统一 `DATA_DIR="${EPIDEMIC_DATA_DIR:-$REPO/data}"`, 四处校验全走它。
- **配套(同日发现, 环境层)**: 生产 crontab 被写成 `30 23 * * *` 且带一行
  `SCAN_TIMEOUT=900` —— 与文档/`run_scan.sh` 头注释的 07:30 不符, 且该变量会**静默把
  所有定时跑批压到 15 分钟**上限(历史成功轮约 5 分钟, 慢一点就必超时)。
  已改回 `30 7 * * *` 并删除该行; crontab 里不再放任何 `SCAN_*` / `HERMES_*`。

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
