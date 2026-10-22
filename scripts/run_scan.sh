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

  --review-channel <渠道>  本轮来自该渠道的会话, 只发该渠道, 等用户确认后才扩散
                          渠道取值: wechat | dingtalk | mail(也认企业微信/钉钉/邮箱)
  --broadcast              本轮是定时任务, 全量推送
  两者都不给                cron 上下文自动按定时任务处理; 其它上下文本轮不推送
EOF
}

MODE=""
SCOPE=""            # review | broadcast | none
SCOPE_CHANNEL=""
STAGE="all"         # all | process(policy 档: 跳过取数, 直接加工 data/raw 已有的存档)
while [ $# -gt 0 ]; do
  case "$1" in
    am|pm|policy) MODE="$1"; shift ;;
    --review-channel)
      if [ $# -lt 2 ]; then echo "[错误] --review-channel 需要渠道参数"; usage; exit 2; fi
      SCOPE="review"; SCOPE_CHANNEL="$2"; shift 2 ;;
    --review-channel=*) SCOPE="review"; SCOPE_CHANNEL="${1#*=}"; shift ;;
    --broadcast) SCOPE="broadcast"; shift ;;
    --stage)
      if [ $# -lt 2 ]; then echo "[错误] --stage 需要参数(all|process)"; usage; exit 2; fi
      STAGE="$2"; shift 2 ;;
    --stage=*) STAGE="${1#*=}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1"; usage; exit 2 ;;
  esac
done
MODE="${MODE:-am}"

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
_BROADCAST_ARMED=0
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
    PROMPT_FETCH="执行 $SCAN_DATE 的政策变化**取数阶段**(运行日期 $SCAN_DATE, 东八区)。本阶段做两件事: 上网找政策存档, 并趁原文在手**就地抽取**。**不要入库、不要出报、不要写对华影响研判** —— 那是下一阶段的事。
【取数纪律】
1) 先读 config/sources.yaml 的 policy_watch_countries 取当天必扫档与轮转档, 再读 skills/global-policy-search/SKILL.md 的取数工具纪律。
2) 优先级固定: collect.py 直抓已知官方入口 > web_fetch > 浏览器。web_search 单轮不超过 15 次; 同一工具失败 2 次就换下一个; 浏览器失败就放弃浏览器, 不要反复重连。
3) WTO ePing 只走列表页。拿到行式列表后立刻用 python3 scripts/parse_eping.py <列表文件> --days 7 预筛, 只对清单内、发布日期在近 7 天内的条目下载存档, 窗口外一律不下。PDF 直链就在列表行里(members.wto.org/crnattachments/...), 不要自己拼。
4) 每找到一条就**立刻**做三件事, 边存边写、绝不留到收尾: ①存档 python3 scripts/collect.py --url <直链> --title <标题>; ②存档成功后**马上**把该条追加写入 data/raw/$SCAN_DATE/candidates.json 并在 search-log.md 记一行; ③趁原文还在手上, 按 prompts/policy-extraction.md 把该条抽成 policy 记录, **立即追加**到 data/raw/$SCAN_DATE/extracted-policy.json。理由: 10-22 实测, 收尾才写 = 白干; 而把抽取留给加工段 = 冷启动重读全部原文(12 万 token), 必超时 —— 取数时就地抽取是零成本, 这正是拆段前单轮流程 3-7 分钟能跑完全程的原因。就地抽取要求: 每条必须有 source.url 与 source.quote(≤120 字, 摘自刚抓到的原文); source.publish_date 用该页面自己的发布日期, 不得用抓取日冒充; 原文没有的信息填 null, **严禁编造**; 旧闻(超 7 天)与第三方站点不写入, 只在 search-log.md 记为线索。当日存档文件数上限 12(含 .meta.json 约等于 6 条), 触顶就停, 剩下的留待下一轮。
【本阶段必须交付的四个文件】
A) 存档: data/raw/$SCAN_DATE/ 下每条一个 html + 一个 meta.json。
B) data/raw/$SCAN_DATE/candidates.json —— 数组, 每条含 title/url/country_en/publish_date/source_name/brief(一句话说明这是什么措施)与 topics(动植物/产品/病害关键词)。**增量维护**: 每存档一条立即追加一条, 不是收尾时一次性写; 哪怕一条都没取到也要创建这个文件(空数组), 下一阶段靠它判断有没有东西可做。
C) data/raw/$SCAN_DATE/extracted-policy.json —— 数组, **每存档一条立即追加一条就地抽取的 policy 记录**(同 B 的边存边写纪律); 哪怕 0 条也要创建空数组。这是下一阶段的主输入, 加工段只补漏、不再重读全部原文。
D) data/raw/$SCAN_DATE/search-log.md —— 逐通道状态, 同样**边扫边记**: 本通道取到几条 / 不可用(附原因, 如 403 封禁、页面结构不匹配、搜索额度用尽)。
【红线】严禁 --broadcast; 严禁逐条穷举下载 PDF; 严禁把第三方站点当 tier=1; 时间到了就**立刻交卷**, 不要为了多找一条而超时。"
    PROMPT_PROCESS="执行 $SCAN_DATE 的政策变化**加工阶段**(运行日期 $SCAN_DATE, 东八区)。取数阶段已存档并就地抽取, 你的输入是**本地文件, 不要联网检索**; 本阶段是轻收尾, 正常几分钟内完成。
1) 读 data/raw/$SCAN_DATE/extracted-policy.json(主输入, 取数段已边存边写生成)。对照 candidates.json 找出「已存档但未抽取」的漏条: 先看 search-log.md 是否已把它记为线索或判为不合格(第三方/旧闻), 确实漏抽的才读对应 html 原文、按 prompts/policy-extraction.md 补抽并追加进 extracted-policy.json —— **不要重读已抽取条目的原文**。extracted-policy.json 与 candidates.json 都为空数组或不存在时, 直接跳到第 4 步(仍要出报)。
2) 入库: python3 scripts/normalize.py --input data/raw/$SCAN_DATE/extracted-policy.json --out data/events/$SCAN_DATE/ --db --max-age-days 7。tier=1 只认官方域名; 第三方站点整条拒收, 只在 search-log.md 记为线索。
3) 研判: 对已入库的 verified 记录逐条写回四维与结论(脚本用法见 prompts/policy-impact.md): python3 scripts/risk.py --event-id <id> --impact-type <约束|机会|中性> --china-relevance <...> --action <...> --rationale <...> --dim trade=N --dim biosecurity=N --dim response=N --dim alignment=N。先用 --list-pending 看还有哪些没写回。
4) 出报(无论前面取到几条, **都必须执行**): python3 scripts/report.py --date $SCAN_DATE --excel --docx。再补写 data/raw/$SCAN_DATE/policy-findings.md。
【红线】严禁 --broadcast; 严禁为了让日报好看而入库旧闻; 严禁编造 quote。**不要在这一步做新的联网检索**, 你只看本地文件。
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
  #   fetch   段 = 上网 + collect.py 存档 + search-log.md   (不抽取、不入库)
  #   process 段 = 只读本地存档 -> 抽取 -> 入库 -> 研判 -> 出报(不联网检索)
  # process 段输入是本地文件, 体量可控, 必然跑得完; fetch 段超时也不影响已存档数据。
  SCAN_TIMEOUT="${SCAN_TIMEOUT:-2400}"
  FETCH_TIMEOUT="${STAGE_FETCH_TIMEOUT:-1500}"
  PROCESS_TIMEOUT="${STAGE_PROCESS_TIMEOUT:-900}"
  _run_agent() {
    # $1=超时秒数 $2=prompt
    eval "timeout -k 30 $1 ${HERMES_RUN_CMD/\{PROMPT\}/$2}"
  }

  if [ "$MODE" = "policy" ] && [ -n "${PROMPT_FETCH:-}" ]; then
    if [ "$STAGE" != "process" ]; then
      echo "[$(date '+%F %T')] 阶段 1/2 取数存档(上限 ${FETCH_TIMEOUT}s)"
      eval "timeout -k 30 ${FETCH_TIMEOUT} ${HERMES_RUN_CMD/\{PROMPT\}/$PROMPT_FETCH}"
      FRC=$?
      if [ "$FRC" -eq 124 ] || [ "$FRC" -eq 137 ]; then
        echo "[失败] 取数阶段超时(${FETCH_TIMEOUT}s), 已终止"
      else
        echo "[$(date '+%F %T')] 阶段 1/2 结束 rc=$FRC, 继续加工出报"
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
    echo "[$(date '+%F %T')] 阶段 2/2 加工出报(上限 ${PROCESS_TIMEOUT}s)"
    _run_agent "$PROCESS_TIMEOUT" "$PROMPT_PROCESS"
    RC=$?
    if [ "$RC" -eq 124 ] || [ "$RC" -eq 137 ]; then
      echo "[失败] 加工阶段超时(${PROCESS_TIMEOUT}s), 已终止"
      RC=1
    fi
  else
    echo "[$(date '+%F %T')] 开始扫描(上限 ${SCAN_TIMEOUT}s)"
    eval "timeout -k 30 ${SCAN_TIMEOUT} ${HERMES_RUN_CMD/\{PROMPT\}/$PROMPT}"
    RC=$?
    if [ "$RC" -eq 124 ] || [ "$RC" -eq 137 ]; then
      echo "[失败] 扫描超时(${SCAN_TIMEOUT}s), 已终止"
      RC=1
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
  RAW_N="$(find "$DATA_DIR/raw/$SCAN_DATE" -maxdepth 1 -type f ! -name '*.meta.json' 2>/dev/null | wc -l | tr -d ' ')"
  if [ "${RAW_N:-0}" -gt 12 ]; then
    MISSING="$MISSING 存档量超限(当轮 ${RAW_N} 个文件 > 12, 疑似穷举下载)"
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
      MISSING="$MISSING 当日入库 0 条(本轮存档 ${RAW_N} 个文件却一条没进库)"
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
