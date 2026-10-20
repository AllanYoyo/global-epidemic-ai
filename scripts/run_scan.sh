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
while [ $# -gt 0 ]; do
  case "$1" in
    am|pm|policy) MODE="$1"; shift ;;
    --review-channel)
      if [ $# -lt 2 ]; then echo "[错误] --review-channel 需要渠道参数"; usage; exit 2; fi
      SCOPE="review"; SCOPE_CHANNEL="$2"; shift 2 ;;
    --review-channel=*) SCOPE="review"; SCOPE_CHANNEL="${1#*=}"; shift ;;
    --broadcast) SCOPE="broadcast"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1"; usage; exit 2 ;;
  esac
done
MODE="${MODE:-am}"

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON_BIN:-python3}"

[ -n "${HOME:-}" ] && [ -f "$HOME/.hermes/.env" ] && . "$HOME/.hermes/.env"

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

# ---- 声明本轮推送范围(落盘成范围锁) ----
# push_scope.py 会校验渠道名: 认不出就报错退出, 绝不静默当成"没设闸门"。
case "$SCOPE" in
  broadcast)
    # 全量推送时清掉可能残留的 REVIEW_CHANNEL: 它与 --broadcast 互斥,
    # .env 里若留着调试用的值会让定时推送直接报错停摆。
    unset REVIEW_CHANNEL
    "$PY" "$REPO/scripts/push_scope.py" --date "$SCAN_DATE" --broadcast \
      --source "run_scan.sh:$MODE:$SCOPE_SOURCE" || exit 1
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

case "$MODE" in
  am) PROMPT="生成 $SCAN_DATE 的政策监测日报: 仅检索外国政府动植物疫情管控政策变化, 完成政策抽取、核验、对华影响研判, 生成 Markdown+Excel+Word 政策日报。$PUSH_INSTRUCTION" ;;
  pm) PROMPT="执行 $SCAN_DATE 的政策晚扫增量: 仅检索外国政府官方检疫/进出口政策变化, 对新增政策完成抽取、核验、影响研判, 并刷新当日政策日报。$PUSH_INSTRUCTION" ;;
  policy) PROMPT="执行 $SCAN_DATE 的政策变化扫描(运行日期 $SCAN_DATE, 东八区): 用 global-policy-search 技能, 按 config/sources.yaml 的 policy_queries 检索 WTO ePing 与外国官方检疫机构的动植物检疫管控政策变化(不采集中国海关总署政策), 只收录发布时间在近 7 天内的新政策(常年挂网的历史公告页与日期不明的页面跳过, 不入库), 抽取为政策记录并完成核验与对华影响研判, 最后用 scripts/report.py --date $SCAN_DATE --excel --docx 生成政策日报。$PUSH_INSTRUCTION" ;;
  *) echo "用法: $0 [am|pm|policy] [--review-channel <渠道> | --broadcast]"; exit 2 ;;
esac

# 周一晨扫: 附加 weekly 档源(EPPO / IPPC / 沙漠蝗旬报等)
if [ "$MODE" = "am" ] && [ "$(date +%u)" = "1" ]; then
  PROMPT="$PROMPT(今天是周一: 请一并检查 sources.yaml 中 weekly 档位的周更源)"
fi

STAMP="$(date '+%F %T')"
echo "[$STAMP] run_scan mode=$MODE date=$SCAN_DATE tz=$TZ scope=$SCOPE src=$SCOPE_SOURCE start"
cd "$REPO"  # agent 依赖仓库相对路径(config/sources.yaml, scripts/*)

if [ -n "${HERMES_RUN_CMD:-}" ]; then
  eval "${HERMES_RUN_CMD/\{PROMPT\}/$PROMPT}"
  RC=$?
  echo "[$(date '+%F %T')] run_scan mode=$MODE date=$SCAN_DATE done rc=$RC"
  exit $RC
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
