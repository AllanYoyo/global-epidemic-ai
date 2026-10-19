#!/usr/bin/env bash
# 疫见全球 · 定时扫描入口(crontab 调用)
#
# 用法: run_scan.sh [am|pm|policy]
#   am     晨扫(全量, 建议 06:30): 完整五段流程, 产出日报并推送; 周一自动附加 weekly 档源
#   pm     晚扫(增量, 建议 18:00): 仅官方源+核心病害, 完成后刷新当日日报
#   policy 政策扫(建议 07:30): 各国动植物检疫/进出口管控政策变化, 产出政策日报
#
# Hermes 调用方式由环境变量 HERMES_RUN_CMD 指定(模板, {PROMPT} 为占位符), 例:
#   HERMES_RUN_CMD='hermes run --prompt "{PROMPT}"'
# 未配置 HERMES_RUN_CMD 时, 退化为向已配置的推送渠道(钉钉/企微)发送"待执行提醒"。
# 环境变量自动加载自 ~/.hermes/.env(若存在)。
set -u

MODE="${1:-am}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON_BIN:-python3}"

[ -n "${HOME:-}" ] && [ -f "$HOME/.hermes/.env" ] && . "$HOME/.hermes/.env"

# 生产 VPS 系统时区是 Etc/UTC, 而 cron 按 Asia/Shanghai 触发。若不显式固定,
# `date +%F` 取到 UTC 日期, 北京时间早间跑批会整体错一天(2026-10 实测: 10-15
# 的扫描产出 2026-10-14-* 报告, 且 report.py 缺 --date 时回退到 UTC 今天)。
export TZ="${SCAN_TZ:-Asia/Shanghai}"
SCAN_DATE="$(date +%F)"

case "$MODE" in
  am) PROMPT="生成 $SCAN_DATE 的政策监测日报: 仅检索外国政府动植物疫情管控政策变化, 完成政策抽取、核验、对华影响研判, 生成 Markdown+Excel+Word 政策日报并推送" ;;
  pm) PROMPT="执行 $SCAN_DATE 的政策晚扫增量: 仅检索外国政府官方检疫/进出口政策变化, 对新增政策完成抽取、核验、影响研判, 并刷新当日政策日报" ;;
  policy) PROMPT="执行 $SCAN_DATE 的政策变化扫描(运行日期 $SCAN_DATE, 东八区): 用 global-policy-search 技能, 按 config/sources.yaml 的 policy_queries 检索 WTO ePing 与外国官方检疫机构的动植物检疫管控政策变化(不采集中国海关总署政策), 只收录发布时间在近 7 天内的新政策(常年挂网的历史公告页与日期不明的页面跳过, 不入库), 抽取为政策记录并完成核验与对华影响研判, 最后用 scripts/report.py --date $SCAN_DATE --excel --docx 生成政策日报并用 scripts/push_report.py --date $SCAN_DATE 推送到已配置渠道" ;;
  *) echo "用法: $0 [am|pm|policy]"; exit 2 ;;
esac

# 周一晨扫: 附加 weekly 档源(EPPO / IPPC / 沙漠蝗旬报等)
if [ "$MODE" = "am" ] && [ "$(date +%u)" = "1" ]; then
  PROMPT="$PROMPT(今天是周一: 请一并检查 sources.yaml 中 weekly 档位的周更源)"
fi

STAMP="$(date '+%F %T')"
echo "[$STAMP] run_scan mode=$MODE date=$SCAN_DATE tz=$TZ start"
cd "$REPO"  # agent 依赖仓库相对路径(config/sources.yaml, scripts/*)

if [ -n "${HERMES_RUN_CMD:-}" ]; then
  eval "${HERMES_RUN_CMD/\{PROMPT\}/$PROMPT}"
  RC=$?
  echo "[$(date '+%F %T')] run_scan mode=$MODE date=$SCAN_DATE done rc=$RC"
  exit $RC
fi

echo "[$(date '+%F %T')] HERMES_RUN_CMD 未配置, 发送待执行提醒"
"$PY" "$REPO/scripts/push_report.py" --message "⏰ 疫见全球·$MODE 扫描待执行($STAMP, 运行日期 $SCAN_DATE)
请对 Hermes 说: $PROMPT" || true
echo "[$(date '+%F %T')] run_scan mode=$MODE fallback-notify done"
