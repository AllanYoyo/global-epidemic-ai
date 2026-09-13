#!/usr/bin/env bash
# 疫见全球 · 定时扫描入口(crontab 调用)
#
# 用法: run_scan.sh [am|pm]
#   am  晨扫(全量, 建议 06:30): 完整五段流程, 产出日报并推送; 周一自动附加 weekly 档源
#   pm  晚扫(增量, 建议 18:00): 仅官方源+核心病害, 完成后刷新当日日报
#
# Hermes 调用方式由环境变量 HERMES_RUN_CMD 指定(模板, {PROMPT} 为占位符), 例:
#   HERMES_RUN_CMD='hermes run --prompt "{PROMPT}"'
# 未配置 HERMES_RUN_CMD 时, 退化为向已配置的推送渠道(钉钉/企微)发送"待执行提醒"。
# 环境变量自动加载自 ~/.hermes/.env(若存在)。
set -u

MODE="${1:-am}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON_BIN:-python3}"

[ -f "$HOME/.hermes/.env" ] && . "$HOME/.hermes/.env"

case "$MODE" in
  am) PROMPT="生成今日疫情日报" ;;
  pm) PROMPT="执行今日疫情晚扫增量: 仅检索 config/sources.yaml 中 schedule 含 pm 的官方源与 core 核心病害的新通报, 对今日新增事件完成抽取、核验、研判, 并刷新今日日报" ;;
  *) echo "用法: $0 [am|pm]"; exit 2 ;;
esac

# 周一晨扫: 附加 weekly 档源(EPPO / IPPC / 沙漠蝗旬报等)
if [ "$MODE" = "am" ] && [ "$(date +%u)" = "1" ]; then
  PROMPT="$PROMPT(今天是周一: 请一并检查 sources.yaml 中 weekly 档位的周更源)"
fi

STAMP="$(date '+%F %T')"
echo "[$STAMP] run_scan mode=$MODE start"

if [ -n "${HERMES_RUN_CMD:-}" ]; then
  eval "${HERMES_RUN_CMD/\{PROMPT\}/$PROMPT}"
  RC=$?
  echo "[$STAMP] run_scan mode=$MODE done rc=$RC"
  exit $RC
fi

echo "[$STAMP] HERMES_RUN_CMD 未配置, 发送待执行提醒"
"$PY" "$REPO/scripts/push_report.py" --message "⏰ 疫见全球·$MODE 扫描待执行($STAMP)
请对 Hermes 说: $PROMPT" || true
echo "[$STAMP] run_scan mode=$MODE fallback-notify done"
