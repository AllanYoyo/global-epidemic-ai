#!/usr/bin/env bash
# 疫见全球 · 定时扫描入口(crontab 调用)
#
# 用法: run_scan.sh [am|pm|policy] [--review-channel <渠道> | --broadcast]
#   am     晨扫(全量, 建议 06:30): 完整五段流程, 产出日报并推送; 周一自动附加 weekly 档源
#   pm     晚扫(增量, 建议 18:00): 仅官方源+核心病害, 完成后刷新当日日报
#   policy 政策扫(建议 07:30): 各国动植物检疫/进出口管控政策变化, 产出政策日报
#
# 推送范围(2026-10-20 钉钉误推事故后引入, 决定性)
# ------------------------------------------------------------------
# 此前推送范围写在 prompt 里让 Agent 临场二选一("渠道会话 → --review-channel,
# 定时任务 → --broadcast"), 而本脚本是 cron 与手动补跑**共用**的入口 —— 手动
# 补跑在 prompt 眼里就是"定时任务那条路", 于是 Agent 选了 --broadcast, 日报发
# 到了钉钉(2026-10-20 实测)。本脚本也确实**没有任何 REVIEW_CHANNEL 传递机制**。
#
# 现在推送范围由**发起方**声明, 并落盘成范围锁(data/reports/.push-scope/<日期>.lock),
# push_report.py 读锁执行, 不再采信 Agent 的临场判断:
#   --review-channel <渠道>  本轮来自该渠道会话 → 只发该渠道; 写 review 范围锁
#   --broadcast              本轮是定时任务     → 全量; 写 broadcast 范围锁
#   两者都不给               → 若是 cron 上下文自动按定时任务处理; 否则**本轮不推送**
#                             (只生成日报), 避免"忘了声明"被当成"发所有渠道"
#
# Hermes 调用方式由环境变量 HERMES_RUN_CMD 指定(模板, {PROMPT} 为占位符), 例:
#   HERMES_RUN_CMD='hermes run --prompt "{PROMPT}"'
# 未配置 HERMES_RUN_CMD 时, 退化为向已配置的推送渠道发送"待执行提醒"(范围同声明)。
# 环境变量自动加载自 ~/.hermes/.env(若存在)。
set -u

usage() {
  cat <<'EOF'
用法: run_scan.sh [am|pm|policy] [--review-channel <渠道> | --broadcast]
                  [--stage all|process|fetch] [--refetch]

  --review-channel <渠道>  本轮来自该渠道的会话, 只发该渠道, 等用户确认后才扩散
                          渠道取值: wechat | dingtalk | mail(也认企业微信/钉钉/邮箱)
  --broadcast              本轮是定时任务, 全量推送
  两者都不给                cron 上下文自动按定时任务处理; 其它上下文本轮不推送
  --stage fetch            只跑取数段(不加工不推送), 结束写 .fetch-done 幂等标记
  --refetch                清除当日取数标记强制重取(与幂等锁配对, 显式留痕)
EOF
}

MODE=""
SCOPE=""            # review | broadcast | none
SCOPE_CHANNEL=""
STAGE="all"         # all | process(policy 档: 跳过取数, 直接加工 data/raw 已有的存档)
                    #    | fetch (policy 档: 只跑取数段, 不加工不推送 —— 会话/人工补数入口)
REFETCH=0           # 1 = 清除当日 .fetch-done 标记, 无视幂等锁强制重取
while [ $# -gt 0 ]; do
  case "$1" in
    am|pm|policy) MODE="$1"; shift ;;
    --review-channel)
      if [ $# -lt 2 ]; then echo "[错误] --review-channel 需要渠道参数"; usage; exit 2; fi
      SCOPE="review"; SCOPE_CHANNEL="$2"; shift 2 ;;
    --review-channel=*) SCOPE="review"; SCOPE_CHANNEL="${1#*=}"; shift ;;
    --broadcast) SCOPE="broadcast"; shift ;;
    --stage)
      if [ $# -lt 2 ]; then echo "[错误] --stage 需要参数(all|process|fetch)"; usage; exit 2; fi
      STAGE="$2"; shift 2 ;;
    --stage=*) STAGE="${1#*=}"; shift ;;
    --refetch) REFETCH=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1"; usage; exit 2 ;;
  esac
done
# 无参数不再默认 am(2026-10-24 修复): 原实现静默按 am 起一轮真实扫描 ——
# 手滑/写错参数的后果是"直接烧一轮 token", 与本仓库 fail-closed 取向相反。
#忘了指定模式的后果必须是"没跑"(马上发现), 而不是"跑了个没料到的模式"。
if [ -z "$MODE" ]; then
  echo "[错误] 未指定模式(am|pm|policy)。"
  usage
  exit 2
fi

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON_BIN:-python3}"

# 调用方显式给的值**优先于** ~/.hermes/.env(后者只是默认配置源)。
# 10-22 实测踩到: source .env 会把外部传入的 HERMES_RUN_CMD 等覆盖掉,
# 于是"用空跑批验证一下"实际上启动了真扫描(两个 Agent 抢资源)。
# 所以先记下调用方给的值, source 之后再还原。
_caller_run_cmd="${HERMES_RUN_CMD-__unset__}"
_caller_health="${HERMES_HEALTH_CMD-__unset__}"
_caller_timeout="${SCAN_TIMEOUT-__unset__}"
_caller_gw_wait="${GATEWAY_WAIT-__unset__}"
_caller_tz="${SCAN_TZ-__unset__}"
[ -n "${HOME:-}" ] && [ -f "$HOME/.hermes/.env" ] && . "$HOME/.hermes/.env"
[ "$_caller_run_cmd" = "__unset__" ] || export HERMES_RUN_CMD="$_caller_run_cmd"
[ "$_caller_health" = "__unset__" ] || export HERMES_HEALTH_CMD="$_caller_health"
[ "$_caller_timeout" = "__unset__" ] || export SCAN_TIMEOUT="$_caller_timeout"
[ "$_caller_gw_wait" = "__unset__" ] || export GATEWAY_WAIT="$_caller_gw_wait"
[ "$_caller_tz" = "__unset__" ] || export SCAN_TZ="$_caller_tz"

# 数据根目录: 与 normalize.py / report.py / push_*.py 保持同一来源。
# 2026-10-22 审计发现: 产物校验曾把路径写死成 "$REPO/data/reports/...", 而其余脚本
# 一律读 EPIDEMIC_DATA_DIR。README 教生产设 EPIDEMIC_DATA_DIR=/opt/global-epidemic-ai/data,
# 一旦照做, Agent 的日报落在 $EPIDEMIC_DATA_DIR 而校验盯着 repo 内 —— 跑批**必然**报
# "缺少: 日报Markdown" 并退出 1, 即 100% 失败。现在统一走 DATA_DIR。
DATA_DIR="${EPIDEMIC_DATA_DIR:-$REPO/data}"

# 生产 VPS 系统时区是 Etc/UTC, 而 cron 按 Asia/Shanghai 触发。若不显式固定,
# `date +%F` 取到 UTC 日期, 北京时间早间跑批会整体错一天(2026-10 实测: 10-15
# 的扫描产出 2026-10-14-* 报告, 且 report.py 缺 --date 时回退到 UTC 今天)。
export TZ="${SCAN_TZ:-Asia/Shanghai}"
SCAN_DATE="$(date +%F)"

# ---- 当日国别/源清单(2026-10-24 引入, 取数段 token 减量的关键一环) ----
# 实测(10-24 state.db): 取数段 prompt 原先让 Agent 自己读 sources.yaml/SKILL.md,
# Agent 临场 read_file 了 58KB 的 AGENTS.md + 16KB 的 SKILL.md —— 全量进上下文
# 且逐轮重发, 是单轮消耗 65k→200k 的主因之一。改为跑批前把**当天相关**的
# 国别/源入口确定性渲染成 ~30 行注入, 取数段不再需要读任何仓库文件。
# 渲染失败(sources.yaml 格式漂移)时退回"允许读 sources.yaml"的旧指令, 不阻塞跑批。
COUNTRY_BLOCK="$("$PY" "$REPO/scripts/scan_targets.py" --date "$SCAN_DATE" 2>/dev/null || true)"
if [ -n "$COUNTRY_BLOCK" ]; then
  # 与 HEALTH_BLOCK 同款清洗: 注入内容可能含 ASCII 双引号/反引号/$(源名/URL),
  # 这些字符经 eval 拼进 hermes -z 会切断引号(10-23 实测), 一律替换/剔除。
  COUNTRY_BLOCK="$(printf '%s' "$COUNTRY_BLOCK" | sed -e 's/"/」/g' -e 's/`//g' -e 's/\$//g')"
else
  COUNTRY_BLOCK="(当日清单渲染失败 —— 本轮允许读 config/sources.yaml 的 policy_watch_countries 段自行确定当日国别)"
fi

# 保守判定"本轮是不是 cron 触发的"。
# 刻意**不把 systemd 算进来**: Hermes 若以 systemd 服务运行, 它拉起的子进程也带
# systemd 祖先, 那样手动/渠道触发的扫描会被误判成定时任务 —— 正是 10-20 事故形态。
_is_cron_context() {
  local pid=$$ ppid comm
  for _ in 1 2 3 4 5 6 7 8; do
    ppid="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')"
    [ -z "$ppid" ] && return 1
    case "$ppid" in *[!0-9]*) return 1 ;; esac
    [ "$ppid" -le 1 ] && return 1
    comm="$(ps -o comm= -p "$ppid" 2>/dev/null | tr -d ' ')"
    case "$comm" in cron|crond) return 0 ;; esac
    pid="$ppid"
  done
  return 1
}

if [ -z "$SCOPE" ]; then
  if _is_cron_context; then
    SCOPE="broadcast"
    SCOPE_SOURCE="cron自动识别"
  else
    SCOPE="none"
    SCOPE_SOURCE="未声明(非定时任务上下文)"
  fi
else
  SCOPE_SOURCE="显式参数"
fi

# --stage fetch 只取数不加工不推送(2026-10-24): 给会话/人工一个**带看护的补数入口** ——
# 它同样享受超时看护/幂等锁/预算刹车, 而不是让调用方自己拼 hermes -z 裸奔。
# 因此强制 scope=none: 不写范围锁、不携带推送指令, 推送范围声明对本轮无意义。
if [ "$MODE" = "policy" ] && [ "$STAGE" = "fetch" ] && [ "$SCOPE" != "none" ]; then
  echo "[WARN] --stage fetch 本轮不加工不推送, 推送范围声明被忽略(scope 强制 none)。"
  SCOPE="none"
  SCOPE_SOURCE="--stage fetch(只取数)"
fi

# 显式 --broadcast 也必须发生在 cron 上下文里(2026-10-20 事故后加固):
# 手动补跑/渠道会话触发时, run_scan.sh 与 cron 走的是同一个入口 —— 允许它自称
# 定时任务, 就等于把"谁发起的"这个判断权又交回给了调用方, 10-20 的钉钉误推
# 正是这么来的。人工运维确需全量补发, 走 push_scope.py 显式声明(见下方报错提示)。
if [ "$SCOPE" = "broadcast" ] && ! _is_cron_context; then
  echo "[错误] 全量推送只能由 cron 调度声明, 本轮不是 cron 上下文(手动/渠道会话触发), 已拒绝。"
  echo "       10-20 事故正是'手动补跑被当成定时任务'导致日报发到了钉钉。"
  echo "       · 渠道会话(先在原渠道完成, 确认后再扩散):"
  echo "         scripts/run_scan.sh $MODE --review-channel <渠道>"
  echo "       · 人工运维确需全量补发(人已确认扩散):"
  echo "         python3 scripts/push_scope.py --date $SCAN_DATE --broadcast --source manual:运维补发"
  echo "         python3 scripts/push_report.py --date $SCAN_DATE --broadcast"
  exit 2
fi

# ---- 声明本轮推送范围(落盘成范围锁) ----
# push_scope.py 会校验渠道名: 认不出就报错退出, 绝不静默当成"没设闸门"。
# 布防标记必须在 case **之前**初始化(set -u 下 EXIT trap 函数要读它)。
# 2026-10-24 修复: 原先初始化写在 case 结束之后, 把 broadcast 分支刚设的 1
# 无条件清零 —— EXIT trap 撤锁在任何退出路径上都空转, 残锁保护形同虚设。
_BROADCAST_ARMED=0
case "$SCOPE" in
  broadcast)
    # 全量推送时清掉可能残留的 REVIEW_CHANNEL: 它与 --broadcast 互斥,
    # .env 里若留着调试用的值会让定时推送直接报错停摆。
    unset REVIEW_CHANNEL
    "$PY" "$REPO/scripts/push_scope.py" --date "$SCAN_DATE" --broadcast \
      --source "run_scan.sh:$MODE:$SCOPE_SOURCE" || exit 1
    # 锁已写下: 从这一刻起, 任何非正常退出都要靠 EXIT trap 把它撤掉。
    _BROADCAST_ARMED=1
    PUSH_INSTRUCTION="推送: 执行 python3 scripts/push_report.py --date $SCAN_DATE --broadcast (本轮是定时任务, 全量范围已声明并写入范围锁; 不要自行判断渠道, 不要改用 --review-channel)。"
    ;;
  review)
    if [ -z "$SCOPE_CHANNEL" ]; then
      echo "[错误] --review-channel 需要渠道参数(wechat | dingtalk | mail)"
      exit 2
    fi
    "$PY" "$REPO/scripts/push_scope.py" --date "$SCAN_DATE" \
      --review-channel "$SCOPE_CHANNEL" --source "run_scan.sh:$MODE:$SCOPE_SOURCE" || exit 1
    # 把渠道真正传给下游: push_report.py 的 REVIEW_CHANNEL 环境变量兜底,
    # Agent 即便漏写 --review-channel 参数, 也只会发这个渠道(范围锁兜底)。
    export REVIEW_CHANNEL="$SCOPE_CHANNEL"
    SCOPE_LABEL="$("$PY" "$REPO/scripts/push_scope.py" --print-label \
      --review-channel "$SCOPE_CHANNEL")" || exit 1
    PUSH_INSTRUCTION="推送: 执行 python3 scripts/push_report.py --date $SCAN_DATE --review-channel $SCOPE_CHANNEL (本轮来自 $SCOPE_LABEL 会话, 只允许发该渠道, 范围锁已写入, 脚本会强制执行; 严禁 --broadcast)。等用户在 $SCOPE_LABEL 确认后, 再执行 python3 scripts/push_report.py --date $SCAN_DATE --review-approved 发完其余渠道。"
    ;;
  none)
    # 不动 REVIEW_CHANNEL: 它若已被声明(渠道会话), 推送最多也只是"发那一个渠道",
    # 属安全收窄; 清掉反而会让用户的渠道会话收不到日报(多一次手动指定)。
    echo "[WARN] 本轮未声明推送范围(非 cron 上下文, 也没给 --review-channel/--broadcast)。"
    echo "       本轮**不扩散**: 严禁 --broadcast。若本轮由用户在某个渠道发起, 只能发"
    echo "       该渠道(见下方推送指令); 否则只生成日报不推送。"
    echo "       定时任务请在 crontab 里显式加 --broadcast。"
    PUSH_INSTRUCTION="推送: 本轮未声明推送范围, **严禁 --broadcast**(全量推送只能由定时任务路径声明)。若本轮由用户在某个渠道发起, 只能发那个渠道: 执行 python3 scripts/push_report.py --date $SCAN_DATE --review-channel <渠道>; 若本轮与任何渠道会话无关, 就只生成日报, 不要推送。"
    ;;
esac

# ---- 广播锁生命周期: 只有本轮真正跑完才保留 ----
# 10-22 审计发现(红线): 撤销逻辑原先只挂在"产物校验 MISSING"这一条分支上。
# 于是**其它所有提前退出**都会把 broadcast 锁留在盘上, 且它 60 分钟内一直放行全量推送:
#   · 网关预检不就绪直接 exit 1(run_scan.sh 的 GATEWAY_WAIT 分支)
#   · 脚本被 SIGTERM/SIGKILL/OOM 杀掉
#   · 任何以后新增的 exit 1 分支
# 这正是 10-20 钉钉误推事故的重演路径, 只是触发点从"跑批没跑完"换成"网关抖动"。
# 现在改成 EXIT trap: 只要本轮没走到"done", 退出(含超时/信号/预检失败)一律撤销。
_revoke_broadcast_lock() {
  if [ "$SCOPE" = "broadcast" ] && [ "$_BROADCAST_ARMED" = "1" ]; then
    if "$PY" "$REPO/scripts/push_scope.py" --date "$SCAN_DATE" --clear >/dev/null 2>&1; then
      echo "[$(date '+%F %T')] run_scan: 本轮未正常完成, 已撤销全量声明(不留残锁)"
    fi
  fi
}
trap _revoke_broadcast_lock EXIT
trap 'exit 143' INT TERM

case "$MODE" in
  am) PROMPT="生成 $SCAN_DATE 的政策监测日报: 仅检索外国政府动植物疫情管控政策变化, 完成政策抽取、核验、对华影响研判, 然后用 scripts/report.py --date $SCAN_DATE --excel --docx 生成 Markdown+Excel+Word 政策日报(两个开关都要带, 漏 --docx 就不会有 Word 简报)。$PUSH_INSTRUCTION" ;;
  pm) PROMPT="执行 $SCAN_DATE 的政策晚扫增量: 仅检索外国政府官方检疫/进出口政策变化, 对新增政策完成抽取、核验、影响研判, 然后用 scripts/report.py --date $SCAN_DATE --excel --docx 刷新当日政策日报(两个开关都要带, 漏 --docx 就不会有 Word 简报)。$PUSH_INSTRUCTION" ;;
  policy)
    # 2026-10-22 结构修复(同日两轮实测失败驱动): 拆成两次独立调用。
    # run3: 检索到 49 条候选, 预算耗尽前 collect/normalize/risk/report 一步没跑。
    # run4: 加了"硬预算 + 边检索边入库"prompt, 落了 3 份存档, 但仍没走到入库与出报。
    # 结论: 单轮作业装不下"跨国检索 + 抽取 + 核验 + 研判 + 出报", 且 prompt 里的
    # 自然语言预算约束不住模型。所以取数与加工分成两段, 中间以 data/raw/<日期>/ 断点。
    #
    # 2026-10-22 深夜再修: 拆段后加工段冷启动要重读全部存档原文(~12 万 token 上下文),
    # 900s 预算当晚三轮实测全超时(旧单轮流程 10-11~10-21 每轮全程仅 3-7 分钟)。
    # 拆段把抽取成本转移成了"冷读全部原文"。恢复旧流程快的根本原因 —— 搜到一条、
    # 趁原文在手就地抽取: 取数段边存边写 extracted-policy.json, 加工段降级为轻收尾
    # (补漏 → 入库 → 研判 → 出报 → 推送)。
    # 2026-10-24 token 体检重写: f7ae1c4 前单轮流程 ~65k/轮, 拆段+纪律后实测
    # 200-250k/轮 —— 差距不在检索量, 在流程复杂度反噬: ①prompt 让 Agent 临场
    # read_file sources.yaml/SKILL.md(实测读了 58KB AGENTS.md + 16KB SKILL.md,
    # 全量进上下文逐轮重发); ②「边存边写」用 python 读改写整个 JSON, 每追加一条
    # 就把已增长的文件读回一次, 还打印整条记录「验证」(实测单次 execute_code
    # 输出 11KB, 红线 2KB 平均超标 50%); ③prompt 越写越长。本次: 当日清单预渲染
    # 注入(见上方 COUNTRY_BLOCK)、写盘全部改 JSONL 追加一行且不打印、严禁读仓库
    # 文件、prompt 本身瘦身。
    # 2026-10-24 P1 再修: 取数单作业不收敛(当天两轮 90/90、两轮 50/50 打满,
    # 打满即整轮作废)——按档位拆组逐个发起, 单组打满只废单组; 通道 HTTP 探测
    # 前移出 Agent(probe_channels.py 确定性预探, 治理后一轮仍有 26 terminal +
    # 25 execute_code 的探测/摘录调用)。模板内 @@COUNTRY_BLOCK@@ 由循环逐组替换。
    PROMPT_FETCH_TPL="执行 $SCAN_DATE 政策变化**取数阶段**(东八区)。只做两件事: 找政策原文存档 + 趁原文在手就地抽取。禁止入库/研判/出报/推送 —— 那是加工段的事。**所有规则都在本 prompt 里, 严禁 read_file 或 cat 任何仓库文件**(当日清单与通道探测结果已在下方注入, SKILL.md/AGENTS.md/scripts 都不需要读, 按本 prompt 字面执行)。
【本组国别与源清单】(当日通道已按档位拆组, 本轮只扫这一组; 清单外通道不扫、不用惦记, 别的组另有轮次)
@@COUNTRY_BLOCK@@
【纪律】
1) 通道探测已由脚本完成(见下方【通道探测结果】): [dead] 通道在 search-log.md 记一行状态后直接跳过, 不再花任何工具调用; 只对 [ok] 且可能有候选的通道继续。下方体检点名的死通道同样照指示跳过或复检一次。仅当下方**没有**【通道探测结果】时才自行批次化探测: 一个 bash for 循环一次探完, 每通道 curl -m 5 -sL -o /dev/null -w '%{http_code} 地址'(地址即 url), 只看汇总状态清单。
2) 抓任何页面**先落盘**(collect.py 或 curl -o), 需要判断内容时只用 python3 scripts/snip.py <存档文件> --grep <关键词>(脚本硬性只回显 ≤2KB); 严禁把整页正文留在对话里; 单次工具输出超过 8KB 即违规。
3) web_search 本段不超过 15 次; 同一工具连败 2 次就换; 浏览器失败就弃用浏览器。
4) WTO ePing 只走列表页: 行式列表存盘后用 python3 scripts/parse_eping.py <列表文件> --days 7 预筛, 只对窗口内条目下载, PDF 直链就在列表行里不要自己拼。
5) 每找到一条**立刻**三步, 不留到收尾:
   ① 存档: python3 scripts/collect.py --url <直链> --title <标题>;
   ② 候选: python3 scripts/jsonl_append.py data/raw/$SCAN_DATE/candidates.jsonl title=<标题> url=<直链> country_en=<英文国名> publish_date=<YYYY-MM-DD> source_name=<源> brief=<一句话> —— 脚本只回执一行(ok +1), **不打印记录内容**; 参数值含空格用单引号括起;
   ③ 抽取: 先用 snip.py 从刚落盘的存档摘标题/发布日期/关键句, 再用 python3 scripts/jsonl_append.py data/raw/$SCAN_DATE/extracted-policy.jsonl 追加一行, 键值模板: record_type=policy title_cn=<中文标题> title_en=<英文标题> country_cn=<国名> country_en=<英文国名> policy_domain=<animal|plant|both|trade|measures> action_type=<收紧|放松|调整|恢复> event_date=<YYYY-MM-DD> effective_date=<YYYY-MM-DD> products=<英文逗号分隔> summary_cn=<中文摘要> source.tier=<1|3> source.name=<源名> source.url=<直链> source.publish_date=<YYYY-MM-DD> source.quote=<≤120字逐字摘录> —— 原文没有的信息留空(存 null), **不打印记录内容**。
   写完不回读、不打印验证 —— 记录对不对由加工段 normalize 校验兜底。
6) 每条必须有 source.url 与 source.quote(≤120 字, 逐字摘自存档文件); source.publish_date 用页面自己的发布日期, 不得用抓取日; 原文没有的信息填 null, 严禁编造; 旧闻(超 7 天)与第三方站点不写入, 只在 search-log.md 记一行线索。
7) 当日存档上限 12 个文件(约 6 条), 触顶即停, 剩余通道记进 search-log.md; 时间到立刻交卷。
【交付】data/raw/$SCAN_DATE/ 下: 存档(html+meta) / candidates.jsonl / extracted-policy.jsonl / search-log.md(逐通道一行状态)。哪怕 0 条也要创建两个空的 .jsonl 文件。
【红线】严禁 --broadcast; 严禁穷举下载 PDF; 严禁第三方站点当 tier=1; 严禁运行 normalize/risk/report/push_report(抢跑会把 first_seen 记错日子)。"
    PROMPT_PROCESS="执行 $SCAN_DATE 政策变化**加工阶段**(东八区)。输入是本地文件, 不联网检索; 轻收尾, 正常几分钟。**严禁 read_file 或 cat 仓库规则文件**(SKILL.md/AGENTS.md/scripts), 用法都在本 prompt。
1) 主输入 data/raw/$SCAN_DATE/extracted-policy.jsonl(逐行 JSON)。对照 candidates.jsonl 找「已存档但未抽取」的漏条: 先看 search-log.md 是否已记为线索或不合格(第三方/旧闻), 确实漏抽的才用 python3 scripts/snip.py 摘该存档原文(硬性 ≤2KB), 再用 python3 scripts/jsonl_append.py data/raw/$SCAN_DATE/extracted-policy.jsonl <键值对> 补抽追加一行(键: record_type/title_cn/title_en/country_cn/country_en/policy_domain/action_type/event_date/effective_date/products/summary_cn/source.tier/source.name/source.url/source.publish_date/source.quote, 原文没有的留空存 null) —— **不要重读已抽取条目的原文, 不要打印记录内容**。两个清单都空或不存在时直接跳到第 4 步(仍要出报)。
2) 入库: python3 scripts/normalize.py --input data/raw/$SCAN_DATE/extracted-policy.jsonl --out data/events/$SCAN_DATE/ --db --max-age-days 7。tier=1 只认官方域名; 第三方站点整条拒收, 只在 search-log.md 记为线索。
3) 研判: 对已入库 verified 记录逐条写回四维与结论, 先用 python3 scripts/risk.py --list-pending 看缺哪些, 再逐条 python3 scripts/risk.py --event-id <id> --impact-type <约束|机会|中性> --china-relevance <直接涉及中国|间接影响|暂无明显关联> --action <...> --rationale <...> --dim trade=N --dim biosecurity=N --dim response=N --dim alignment=N。
4) 出报(无论几条都必须执行): python3 scripts/report.py --date $SCAN_DATE --excel --docx; 补写 data/raw/$SCAN_DATE/policy-findings.md。
【红线】严禁 --broadcast; 严禁为了让日报好看而入库旧闻; 严禁编造 quote; 不做新的联网检索。
$PUSH_INSTRUCTION"
    PROMPT="$PROMPT_PROCESS" ;;
  *) echo "用法: $0 [am|pm|policy] [--review-channel <渠道> | --broadcast]"; exit 2 ;;
esac

# 周一晨扫: 附加 weekly 档源(EPPO / IPPC / 沙漠蝗旬报等)
if [ "$MODE" = "am" ] && [ "$(date +%u)" = "1" ]; then
  PROMPT="$PROMPT(今天是周一: 请一并检查 sources.yaml 中 weekly 档位的周更源)"
fi

STAMP="$(date '+%F %T')"
echo "[$STAMP] run_scan mode=$MODE date=$SCAN_DATE tz=$TZ scope=$SCOPE src=$SCOPE_SOURCE start"
cd "$REPO"  # agent 依赖仓库相对路径(config/sources.yaml, scripts/*)

# ---- 跑批开始的时间戳: 产物新鲜度判定用(同名文件可能是上一轮留下的) ----
SCAN_START_EPOCH="$(date +%s)"

# 产物是否"这一轮才生成的": 存在、非空、且 mtime 不早于跑批开始。
_fresh() {
  [ -s "$1" ] || return 1
  local m
  m="$(stat -c %Y "$1" 2>/dev/null)" || return 1
  [ "$m" -ge "$SCAN_START_EPOCH" ] || return 1
  return 0
}

if [ -n "${HERMES_RUN_CMD:-}" ]; then
  # ---- 网关健康预检(2026-10-22 加固) ----
  # 10-20 实测: cron 恰好撞上网关重启(Gateway draining), 整轮扫描 8 秒就废了。
  # 跑批前先探一次; 不就绪就短暂等待后重试(重启窗口通常几十秒),
  # 一直不就绪就明确失败退出 —— 而不是把一整轮扫描耗在必然失败的重试上。
  HEALTH_CMD="${HERMES_HEALTH_CMD:-hermes status}"
  if command -v hermes >/dev/null 2>&1; then
    GATEWAY_WAIT="${GATEWAY_WAIT:-180}"
    WAITED=0
    while :; do
      GW_LINE="$(timeout -k 5 20 $HEALTH_CMD 2>&1 | grep -E 'Gateway:' | head -1)"
      if [ -z "$GW_LINE" ]; then
        echo "[$(date '+%F %T')] 网关状态无法解析, 跳过预检继续"
        break
      fi
      case "$GW_LINE" in
        *"not running"*|*"draining"*|*"stopped"*|*"failed"*|*unavailable*)
          if [ "$WAITED" -ge "$GATEWAY_WAIT" ]; then
            echo "[失败] 网关在 ${GATEWAY_WAIT}s 内始终不就绪($GW_LINE), 本轮放弃。"
            echo "       10-20 实测: 撞上网关重启(Gateway draining)时整轮扫描直接废掉。"
            exit 1
          fi
          sleep 15
          WAITED=$((WAITED + 15))
          echo "[$(date '+%F %T')] 网关未就绪($GW_LINE), 等待重试... ${WAITED}s/${GATEWAY_WAIT}s"
          ;;
        *) break ;;   # running / 解析不出关键字 → 视为就绪, 不拦
      esac
    done
  else
    echo "[WARN] 找不到 hermes 命令, 跳过网关健康预检"
  fi

  # ---- 看门狗: 跑批超时直接终止 ----
  # 10-22 实测: 检索阶段卡死 44 分钟(web_search 撞单轮上限, Agent 反复重试到
  # 自行放弃), 白烧 40 多分钟 CPU。给硬上限, 超时即 kill 并报失败。
  #
  # 2026-10-22 结构修复: policy 档拆成**两次独立 Hermes 调用**。
  # 实测(同日两轮, run3/run4)单轮作业无论 prompt 怎么写都会失败: Agent 把整轮
  # 预算都花在检索上, normalize/risk/report 一步都跑不到 —— run3 拿到 49 条候选、
  # run4 落了 3 份存档, 两轮都是 0 入库、0 日报。prompt 里的"硬预算"是自然语言,
  # 约束不住模型。拆开后每段各自独立计时、独立收尾:
  #   fetch   段 = 上网(批次化探测+落盘) + collect.py 存档 + 就地抽取(从存档
  #               文件摘录) + search-log/candidates/extracted-policy 边存边写
  #               (不入库、不出报)
  #   process 段 = 只读 extracted-policy.json 补漏 -> 入库 -> 研判 -> 出报(不联网)
  # process 段输入是本地文件, 体量可控, 必然跑得完; fetch 段超时也不影响已存档数据。
  SCAN_TIMEOUT="${SCAN_TIMEOUT:-2400}"
  FETCH_TIMEOUT="${STAGE_FETCH_TIMEOUT:-1500}"
  PROCESS_TIMEOUT="${STAGE_PROCESS_TIMEOUT:-900}"
  _run_agent() {
    # $1=超时秒数 $2=prompt
    eval "timeout -k 30 $1 ${HERMES_RUN_CMD/\{PROMPT\}/$2}"
  }

  # ---- 孤儿 turn 终止(2026-10-23, 受控实验驱动) ----
  # `hermes -z` 只是把 prompt 经 websocket 提交给网关, turn 由网关守护进程执行;
  # timeout 杀掉的 CLI 只是断开 ws, 网关侧照跑到 max_turns —— 10-22 实测孤儿跑满
  # 90 轮(~8M token), 10-23 取数段孤儿超时后仍多跑并抢跑 normalize。网关没有按
  # 会话终止的 CLI(pause 只挡新 turn, sessions 无 stop, 控制套接字无 interrupt
  # 动词), 看门狗又是"不活动"触发 —— 活跃的孤儿永远打不断。唯一确定性手段是
  # restart: restart_drain_timeout=0 立即硬中断所有在跑 turn; 被中断会话按
  # Hermes 语义"下条消息续跑", 用户微信在途消息只是晚点回, 影响有限。
  # SCAN_ORPHAN_RESTART=0 可关闭。
  _abort_gateway_orphans() {
    [ "${SCAN_ORPHAN_RESTART:-1}" = "1" ] || return 0
    command -v hermes >/dev/null 2>&1 || return 0
    echo "[$(date '+%F %T')] [孤儿清理] 阶段被 timeout 终止, 网关侧 turn 仍在跑 —— 重启网关硬中断"
    # 600s 上限: 实测重启 ~164s(新进程启动慢), CLI 内部对"在跑 turn+drain"的等待
    # 上限是 2745s —— 套壳防极端情况挂住整轮 cron(重启本身是 systemctl 级, 超时
    # 杀掉的只是等待进程, 重启仍会完成)。
    if timeout -k 30 600 hermes gateway restart >/dev/null 2>&1; then
      echo "[$(date '+%F %T')] [孤儿清理] 网关已重启"
    else
      echo "[WARN] 网关重启命令未按时返回(重启可能仍在后台进行), 继续就绪等待"
    fi
    # 后续加工段要立刻用网关, 就绪等待(与预检同款判定; 实测启动最慢 ~165s)
    _waited=0
    _ready=0
    while [ "$_waited" -lt 180 ]; do
      _gw="$(timeout -k 5 20 hermes status 2>&1 | grep 'Gateway:' | head -1)"
      case "$_gw" in
        *"✓ running"*) _ready=1; break ;;
      esac
      sleep 5
      _waited=$((_waited + 5))
    done
    if [ "$_ready" -eq 1 ]; then
      echo "[$(date '+%F %T')] [孤儿清理] 网关就绪(等待 ${_waited}s)"
    else
      echo "[WARN] 网关重启后 ${_waited}s 仍未就绪, 下一段大概率失败 —— 这轮算废, 靠产物校验兜底"
    fi
  }

  if [ "$MODE" = "policy" ] && [ -n "${PROMPT_FETCH_TPL:-}" ]; then
    # ---- 取数幂等锁 + 预算刹车(2026-10-24 token 爆量事故后引入) ----
    # 10-24 现场: 6 条存档 09:39 已全部落盘, 之后 12:50/13:54/14:55 三轮重复取数
    # 零新增, ~126 万输入 token + 3200 万 cache 读白烧, 当日 Token Plan 打穿(429)。
    # 两道闸: ①.fetch-done 标记 —— 当日取数已交付(rc=0 或已有存档)就不再重跑,
    # 强制重取必须显式 --refetch; ②token_budget.py —— 东八区当日 Hermes 消耗
    # 达到预算就不再启动新取数, 本轮降级为"只加工已有存档"(数据不丢, 损失止于增量)。
    FETCH_DIR="$DATA_DIR/raw/$SCAN_DATE"
    FETCH_DONE="$FETCH_DIR/.fetch-done"
    FETCH_SKIP=""
    FETCH_RAN=0
    FRC=0
    if [ "$STAGE" != "process" ]; then
      echo "[$(date '+%F %T')] 预算检查(东八区当日 Hermes 消耗):"
      if ! "$PY" "$REPO/scripts/token_budget.py" --date "$SCAN_DATE"; then
        FETCH_SKIP="budget"
        mkdir -p "$FETCH_DIR" 2>/dev/null || true
        printf -- '- 预算刹车: 东八区当日 Hermes 消耗已达上限, 本轮跳过取数(%s)\n' \
          "$(date '+%F %T')" >> "$FETCH_DIR/search-log.md" 2>/dev/null || true
      fi
      if [ -z "$FETCH_SKIP" ] && [ "$REFETCH" = "1" ]; then
        rm -f "$FETCH_DONE"
        echo "[$(date '+%F %T')] --refetch: 已清除当日取数标记, 本轮强制重取"
      fi
      if [ -z "$FETCH_SKIP" ] && [ -f "$FETCH_DONE" ]; then
        FETCH_SKIP="fetched"
        echo "[INFO] 当日取数已完成, 标记内容:"
        sed 's/^/       /' "$FETCH_DONE" 2>/dev/null || true
      fi
      if [ -n "$FETCH_SKIP" ]; then
        echo "[WARN] 跳过取数段(原因: $FETCH_SKIP), 直接用 data/raw/$SCAN_DATE/ 已有存档加工。"
        echo "       如确需重取: scripts/run_scan.sh policy --refetch"
      else
      # ---- 死通道跳过名单(2026-10-23 引入, 确定性) ----
      # 10-22/10-23 实测: 约 20 个通道连续两日一字不差地不可用(403/连接失败/
      # 占位页), 取数段每天仍要花 30-50 轮重新发现它们。channel_health.py 从近
      # 几日 search-log.md 提取名单注入 prompt(只读、不联网、失败不阻塞跑批)。
      HEALTH_BLOCK="$("$PY" "$REPO/scripts/channel_health.py" --date "$SCAN_DATE" 2>/dev/null || true)"
      if [ -n "$HEALTH_BLOCK" ]; then
        # 名单内容摘自 search-log 原文, 可能含 ASCII 双引号/反引号/$(如 返回"관리모드"
        # 占位页)。prompt 最终要经 eval 拼进 hermes -z "...", 这些字符会切断引号把
        # 命令拆烂(实测 10-23 复查: 体检名单带引号 → -z 参数被截断)。一律替换/剔除。
        HEALTH_BLOCK="$(printf '%s' "$HEALTH_BLOCK" | sed -e 's/"/」/g' -e 's/`//g' -e 's/\$//g')"
        echo "[$(date '+%F %T')] 已注入通道体检名单($(echo "$HEALTH_BLOCK" | grep -c '^-' ) 条)"
      fi
      # ---- 国别分组取数(2026-10-24 P1): 单作业装不下全部通道, 按档位拆组 ----
      # 实证: 当天两轮 90/90、两轮 50/50 打满 max_turns, 打满即整轮作废重来;
      # 拆组后单组打满只废单组(存档边存边写, 其它组的成果不受牵连)。
      # 超时按各组源对数加权(保底 360s), 总时长仍受 FETCH_TIMEOUT 约束。
      GROUPS_INFO="$("$PY" "$REPO/scripts/scan_targets.py" --date "$SCAN_DATE" --groups 2>/dev/null || true)"
      G1_N="$(printf '%s' "$GROUPS_INFO" | sed -n 's/^n1=\([0-9][0-9]*\) n2=[0-9][0-9]*$/\1/p')"
      G2_N="$(printf '%s' "$GROUPS_INFO" | sed -n 's/^n1=[0-9][0-9]* n2=\([0-9][0-9]*\)$/\1/p')"
      GROUP_LIST=""
      case "${G1_N:-x}" in [0-9]*) [ "$G1_N" -gt 0 ] && GROUP_LIST="1" ;; esac
      case "${G2_N:-x}" in [0-9]*) [ "$G2_N" -gt 0 ] && GROUP_LIST="$GROUP_LIST 2" ;; esac
      if [ -z "$GROUP_LIST" ]; then
        # 分组解析失败(格式漂移) → 退回单组全量旧口径, 不阻塞跑批
        GROUP_LIST="x"
        echo "[WARN] 当日分组解析失败(收到: $GROUPS_INFO), 本轮退回单组全量取数"
      fi
      GROUP_TOTAL="$(echo $GROUP_LIST | wc -w | tr -d ' ')"
      N_TOTAL=$(( ${G1_N:-0} + ${G2_N:-0} ))
      FRC=0
      for G in $GROUP_LIST; do
        if [ "$G" = "x" ]; then
          GROUP_BLOCK="$COUNTRY_BLOCK"
          PROBE_BLOCK="$("$PY" "$REPO/scripts/probe_channels.py" --date "$SCAN_DATE" 2>/dev/null || true)"
          G_N="$N_TOTAL"
        else
          GROUP_BLOCK="$("$PY" "$REPO/scripts/scan_targets.py" --date "$SCAN_DATE" --group "$G" 2>/dev/null || true)"
          PROBE_BLOCK="$("$PY" "$REPO/scripts/probe_channels.py" --date "$SCAN_DATE" --group "$G" 2>/dev/null || true)"
          case "$G" in 1) G_N="$G1_N" ;; 2) G_N="$G2_N" ;; esac
        fi
        # 两道清洗同 COUNTRY_BLOCK: 注入内容不得含 ASCII 双引号/反引号/$
        GROUP_BLOCK="$(printf '%s' "$GROUP_BLOCK" | sed -e 's/"/」/g' -e 's/`//g' -e 's/\$//g')"
        PROBE_BLOCK="$(printf '%s' "$PROBE_BLOCK" | sed -e 's/"/」/g' -e 's/`//g' -e 's/\$//g')"
        if [ -z "$GROUP_BLOCK" ]; then
          echo "[WARN] 组$G 清单渲染为空, 跳过该组"
          continue
        fi
        if [ "$GROUP_TOTAL" -le 1 ] || [ "${G_N:-0}" -le 0 ] || [ "$N_TOTAL" -le 0 ]; then
          G_TIMEOUT="$FETCH_TIMEOUT"
        else
          G_TIMEOUT=$(( FETCH_TIMEOUT * G_N / N_TOTAL ))
          [ "$G_TIMEOUT" -lt 360 ] && G_TIMEOUT=360
        fi
        PROMPT_FETCH="${PROMPT_FETCH_TPL/@@COUNTRY_BLOCK@@/$GROUP_BLOCK}"
        [ -n "$PROBE_BLOCK" ] && PROMPT_FETCH="$PROMPT_FETCH
$PROBE_BLOCK"
        [ -n "$HEALTH_BLOCK" ] && PROMPT_FETCH="$PROMPT_FETCH
$HEALTH_BLOCK"
        echo "[$(date '+%F %T')] 阶段 1/2 取数存档 · 组$G/$GROUP_TOTAL(源对 ${G_N:-?}, 上限 ${G_TIMEOUT}s)"
        FETCH_RAN=1
        eval "timeout -k 30 ${G_TIMEOUT} ${HERMES_RUN_CMD/\{PROMPT\}/$PROMPT_FETCH}"
        GRC=$?
        [ "$GRC" -gt "$FRC" ] && FRC="$GRC"
        if [ "$GRC" -eq 124 ] || [ "$GRC" -eq 137 ]; then
          echo "[失败] 组$G 取数超时(${G_TIMEOUT}s), 已终止"
          _abort_gateway_orphans
        else
          echo "[$(date '+%F %T')] 组$G 取数结束 rc=$GRC"
        fi
      done
      if [ "$FRC" -eq 0 ]; then
        echo "[$(date '+%F %T')] 阶段 1/2 全部组结束 rc=0, 继续加工出报"
      fi
      fi
    else
      echo "[$(date '+%F %T')] 指定 --stage process: 跳过取数, 直接用 data/raw/$SCAN_DATE/ 已有存档加工"
    fi
    # 确定性兜底(2026-10-22): 取数段 Agent 每轮都在写 candidates.json 前耗尽工具
    # 预算, 加工段按"文件不存在就跳过"直接空转 —— 6 条已存档原文一条没进库。
    # 现在取数段结束后(无论成败, --stage process 补跑同样)从 meta.json 无损合成
    # 最小版候选清单, 保证加工段永远拿得到存档 url/title; 语义字段由加工段照
    # 抽取规则从原文补齐。脚本幂等、不联网、不编造(见 synth_candidates.py)。
    "$PY" "$REPO/scripts/synth_candidates.py" --date "$SCAN_DATE" || \
      echo "[WARN] synth_candidates 失败(不影响继续, 加工段将看不到候选清单兜底)"
    # ---- 取数完成标记: 只有本轮真的跑了取数才写(跳过的不改写原标记的完成时刻) ----
    # rc=0(正常收尾, 哪怕 0 条的安静日)或已有存档(取到了东西, 值得保留断点)才写;
    # 超时且 0 存档 → 不写, 下一轮允许重取。
    if [ "$FETCH_RAN" = "1" ]; then
      RAW_N_NOW="$(find "$FETCH_DIR" -maxdepth 1 -type f -name '*.meta.json' 2>/dev/null | wc -l | tr -d ' ')"
      CAND_N="$(wc -l < "$FETCH_DIR/candidates.jsonl" 2>/dev/null | tr -d ' ')"
      case "$CAND_N" in ''|*[!0-9]*) CAND_N=0 ;; esac
      if [ "$FRC" -eq 0 ] || [ "${RAW_N_NOW:-0}" -gt 0 ]; then
        mkdir -p "$FETCH_DIR" 2>/dev/null || true
        { echo "done_at=$(date '+%F %T')"; echo "fetch_rc=$FRC";
          echo "archives=$RAW_N_NOW"; echo "candidates=$CAND_N"; } > "$FETCH_DONE"
        echo "[$(date '+%F %T')] 取数标记已写($FETCH_DONE): rc=$FRC 存档=$RAW_N_NOW 候选=$CAND_N"
      else
        echo "[WARN] 本轮取数 rc=$FRC 且 0 存档, 不写完成标记(下一轮可重取)"
      fi
    fi
    # --stage fetch 到此为止: 不加工不推送, 不做日报产物校验(那两样是全流程的判据)。
    if [ "$STAGE" = "fetch" ]; then
      if [ -f "$FETCH_DONE" ]; then
        echo "[$(date '+%F %T')] run_scan --stage fetch 完成(未加工未推送); 加工请跑: scripts/run_scan.sh policy --stage process"
        _BROADCAST_ARMED=0
        exit 0
      fi
      echo "[失败] --stage fetch 未正常结束且无存档(未写取数标记)"
      exit 1
    fi
    echo "[$(date '+%F %T')] 阶段 2/2 加工出报(上限 ${PROCESS_TIMEOUT}s)"
    _run_agent "$PROCESS_TIMEOUT" "$PROMPT_PROCESS"
    RC=$?
    if [ "$RC" -eq 124 ] || [ "$RC" -eq 137 ]; then
      echo "[失败] 加工阶段超时(${PROCESS_TIMEOUT}s), 已终止"
      RC=1
      _abort_gateway_orphans
    fi
  else
    echo "[$(date '+%F %T')] 开始扫描(上限 ${SCAN_TIMEOUT}s)"
    eval "timeout -k 30 ${SCAN_TIMEOUT} ${HERMES_RUN_CMD/\{PROMPT\}/$PROMPT}"
    RC=$?
    if [ "$RC" -eq 124 ] || [ "$RC" -eq 137 ]; then
      echo "[失败] 扫描超时(${SCAN_TIMEOUT}s), 已终止"
      RC=1
      _abort_gateway_orphans
    fi
  fi

  # ---- 产物校验(2026-10-22 加固): 退出码不能信, 只能信产物 ----
  # Agent 放弃时退出码仍是 0(10-22 实测: 检索卡死 44 分钟, Agent 自行放弃, rc=0),
  # run_scan.sh 直接透传 → cron 记成功 → **没有任何告警**, 故障静默一整天。
  # 所以跑完必须核对这一轮到底产出了什么, 且产物必须是"这一轮新生成"的。
  REPORT_MD="$DATA_DIR/reports/$SCAN_DATE-policy-report.md"
  PUSH_FLAG="$DATA_DIR/reports/.pushed/$SCAN_DATE.flag"
  PENDING_FLAG="$DATA_DIR/reports/.review-pending/$SCAN_DATE.flag"
  MISSING=""
  _fresh "$REPORT_MD" || MISSING="$MISSING 日报Markdown"
  case "$SCOPE" in
    broadcast) _fresh "$PUSH_FLAG" || MISSING="$MISSING 推送标记(.pushed)" ;;
    review)    _fresh "$PENDING_FLAG" || MISSING="$MISSING 审阅待审标记(.review-pending)" ;;
    none)      : ;;   # 本轮不扩散, 只要求日报产物
  esac
  # 存档量上限(2026-10-22): 正常一轮只存 2 个文件(10-21 实测); 存了 20+ 个说明
  # Agent 在穷举下载(逐条下 WTO 通报 PDF 核发布日期)—— 10-22 就是这么跑的,
  # 40 分钟没完。这道终检兜住"绕过 collect.py 直接往 raw 目录倾倒"的情况。
  # 2026-10-24 修正: 原先「数所有非 meta 文件」把取数段强制创建的交付文件
  # (candidates.json / extracted-policy.json / search-log.md, prompt 要求哪怕
  # 0 条也要建空数组)与加工段产物(policy-findings.md)全数进去 —— 安静的一天
  # RAW_N 也 ≥3, 「当日有存档却 0 入库」恒触发, 每天必红。改为只数
  # *.meta.json(collect.py 每条存档恰写一个), 计数即真实存档条数, 交付文件天然排除。
  RAW_N="$(find "$DATA_DIR/raw/$SCAN_DATE" -maxdepth 1 -type f -name '*.meta.json' 2>/dev/null | wc -l | tr -d ' ')"
  if [ "${RAW_N:-0}" -gt 12 ]; then
    MISSING="$MISSING 存档量超限(当轮 ${RAW_N} 条存档 > 12, 疑似穷举下载)"
  fi
  # ---- 当日入库条数终检(2026-10-22, 假成功的最后一道口子) ----
  # 实机复现: 阶段 2 加工超时被杀, 但 Agent 在被杀前一刻跑完了 report.py, 于是
  # 日报 md/xlsx/docx 三件套齐全且新鲜 -> 产物校验通过 -> rc=0「跑批成功」,
  # 而库里当日入库是 **0 条**。cron 记成功、监控看不出来, 正是 10-22 那次的形态。
  # 只查文件存在性永远抓不到这种情况, 必须查**有没有真的入库**。
  # 判定规则(区分"安静的一天"与"流程断了"):
  #   当日有存档 但入库 0 条 -> 判失败(取到了东西却没走完入库, 流程中断)
  #   当日无存档 且入库 0 条 -> 允许成功(确实没有新政策)
  if [ "${RAW_N:-0}" -gt 0 ]; then
    NEW_N="$("$PY" "$REPO/scripts/normalize.py" --count-first-seen "$SCAN_DATE" 2>/dev/null | tail -1)"
    case "$NEW_N" in
      ''|*[!0-9]*) NEW_N=0 ;;
    esac
    if [ "$NEW_N" -eq 0 ]; then
      MISSING="$MISSING 当日入库 0 条(本轮存档 ${RAW_N} 条却一条没进库)"
    else
      echo "[$(date '+%F %T')] 当日入库计数: $NEW_N 条"
    fi
  fi
  if [ -n "$MISSING" ]; then
    echo "[失败] 本轮扫描未完成, 缺少:$MISSING"
    echo "       (Agent 放弃时退出码仍是 0, 不能只看 rc; 产物必须是这一轮新生成的)"
    # 失败即撤销声明: 这一轮没推成, 全量声明就不能留着放行之后的人工全量推送
    # (10-22 实测: cron 07:30 写下 broadcast 锁, 那一轮检索卡死后没推成, 锁却
    #  留在盘上, 当天任何时候 push_report.py --broadcast 都会被它放行 → 群发
    #  到钉钉, 正是 10-20 事故的重演路径)。
    if [ "$SCOPE" = "broadcast" ]; then
      "$PY" "$REPO/scripts/push_scope.py" --date "$SCAN_DATE" --clear \
        && echo "[$(date '+%F %T')] run_scan: 本轮未推送, 已撤销全量声明(不留残锁)"
    fi
    exit 1
  fi

  # ---- Agent 退出码同样不能被产物覆盖(2026-10-22) ----
  # 实机复现: 阶段 2 加工阶段超时被杀, 日志已打 [失败] 加工阶段超时(900s), 但紧接着
  # 又输出 done rc=0 —— 因为成功路径无条件 exit 0, 把 RC=1 吞了。结果是"超时失败"
  # 被记成"跑批成功"。产物齐全只说明 report.py 跑过, 不代表 Agent 跑完了。
  if [ "$RC" -ne 0 ]; then
    echo "[失败] Agent 退出码 $RC, 本轮判定失败(即便产物齐全也不放过)"
    if [ "$SCOPE" = "broadcast" ]; then
      "$PY" "$REPO/scripts/push_scope.py" --date "$SCAN_DATE" --clear >/dev/null 2>&1 \
        && echo "[$(date '+%F %T')] run_scan: 已撤销全量声明(不留残锁)"
    fi
    exit 1
  fi

  # 确定性补齐日报产物(2026-10-20 加固): Word 日报是 `report.py --docx` 生成的,
  # 而那一步由 Agent 照 prompt 执行 —— 实测 10-20 的跑批漏掉了 --docx, 只产出
  # csv/md/xlsx, 而推送侧当时是静默跳过附件(用户收到纯文本却以为带 Word)。
  # 校验已确认 Markdown 存在, 此时补出来的 Word 才是真日报(而不是空壳)。
  "$PY" "$REPO/scripts/report.py" --date "$SCAN_DATE" --ensure-docx \
    || echo "[$(date '+%F %T')] run_scan: Word 日报补生成失败(推送将只带正文)"
  # 产物校验通过 = 本轮真的跑完了, 广播声明可以保留给之后的推送/补跑。
  _BROADCAST_ARMED=0
  echo "[$(date '+%F %T')] run_scan mode=$MODE date=$SCAN_DATE done rc=0 (产物校验通过)"
  exit 0
fi

echo "[$(date '+%F %T')] HERMES_RUN_CMD 未配置, 发送待执行提醒"
# 提醒的范围与本轮声明一致: 渠道会话只提醒审阅渠道, 定时任务才群发。
# 未声明范围(none)时不发提醒 —— 推送范围没声明, 就不往任何渠道发东西。
case "$SCOPE" in
  broadcast)
    "$PY" "$REPO/scripts/push_report.py" --broadcast --message "⏰ 疫见全球·$MODE 扫描待执行($STAMP, 运行日期 $SCAN_DATE)
请对 Hermes 说: $PROMPT" || true
    ;;
  review)
    "$PY" "$REPO/scripts/push_report.py" --review-channel "$SCOPE_CHANNEL" --message "⏰ 疫见全球·$MODE 扫描待执行($STAMP, 运行日期 $SCAN_DATE)
请对 Hermes 说: $PROMPT" || true
    ;;
  *)
    echo "[$(date '+%F %T')] 未声明推送范围, 跳过待执行提醒(见上方 WARN)"
    ;;
esac
echo "[$(date '+%F %T')] run_scan mode=$MODE fallback-notify done"
