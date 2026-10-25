#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""疫见全球 · 通道确定性预探(2026-10-24 P1 引入, 取数段拆小配套)

为什么需要它: 取数段 prompt 的「批次化探测」纪律(2026-10-23)仍要把探测结果
**留在对话里** —— 10-24 实测治理后一轮仍有 26 次 terminal + 25 次
execute_code, 逐轮全量重发上下文让单轮 cache 读高达 5M。探测是纯 HTTP
状态检查, 本不需要模型参与: 本脚本在取数段启动前用 curl 并行探完当日
全部源入口, 把 ≤2KB 的状态清单注入 prompt, Agent 对 [dead] 通道直接跳过
(记 search-log 一行), 只对 [ok] 通道做语义筛选与取数。

口径:
- 每个源入口 URL 一次 GET(-m 6 超时, 8 并发), 只看 HTTP 状态码, 不下载正文;
- 200-399 = ok; 其余(含 000=连接失败/超时) = dead —— 与 channel_health.py
  从 search-log 归纳的「不可用」同口径(403/占位页/连接失败);
- 输出按 sid 一行, 超过 48 行截断; 输出经与 channel_health 相同的清洗
  (无 ASCII 双引号/反引号/$), run_scan.sh 注入处还有第二层;
- 幂等、只读、不编造; curl 缺失/全部失败时输出告警行但不报错(不阻塞跑批,
  Agent 退回自行探测的旧纪律)。

用法(由 run_scan.sh 逐组调用):
  python3 scripts/probe_channels.py --date 2026-10-24           # 当日全部源
  python3 scripts/probe_channels.py --date 2026-10-24 --group 1 # 只探必扫档组
"""

import argparse
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from normalize import biz_today  # 业务时区口径一致
import scan_targets

UA = "Mozilla/5.0 (compatible; policy-monitor/1.0; +official-sources-check)"
TIMEOUT_SECS = 6
MAX_WORKERS = 8
MAX_LINES = 48


def classify(code):
    """HTTP 状态码 → ok/dead。000=连接失败/超时。"""
    if 200 <= code < 400:
        return "ok"
    return "dead"


def _clean(s):
    """与 channel_health 同款清洗: 输出会被拼进 prompt 经 eval 送 hermes -z。"""
    return str(s).replace('"', "」").replace("`", "").replace("$", "")


def probe_one(sid, url):
    """探一个源入口, 返回 (sid, 状态码int, ok/dead)。curl 不可用等异常按 dead。"""
    if not url.startswith(("http://", "https://")):
        return sid, 0, "dead"
    try:
        p = subprocess.run(
            ["curl", "-m", str(TIMEOUT_SECS), "-sS", "-o", "/dev/null",
             "-A", UA, "-w", "%{http_code}", "-L", url],
            capture_output=True, text=True, timeout=TIMEOUT_SECS + 4)
        code = int(p.stdout.strip() or 0)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        code = 0
    return sid, code, classify(code)


def probe(date_str, group=None):
    """探当日(或单组)全部源入口, 返回 [(sid, code, state)] —— 按 sid 去重。"""
    picked = scan_targets._pick(date_str, group)
    seen = {}
    for _tier, _cn, _en, sid, url in picked:
        if sid not in seen:
            seen[sid] = url
    items = sorted(seen.items())
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(items))) as ex:
        return list(ex.map(lambda kv: probe_one(kv[0], kv[1]), items))


def render_probe(date_str, group=None):
    """返回可注入 prompt 的文本; 探测失败返回告警行(不阻塞)。"""
    import shutil
    if shutil.which("curl") is None:
        return "[通道探测] 本机无 curl, 未预探 —— 退回自行批次化探测(每通道 curl -m 5 一次)"
    results = probe(date_str, group)
    if not results:
        return ""
    lines = ["【通道探测结果】(确定性脚本已预探; [dead] 通道直接跳过并在 search-log 记一行, [ok] 才继续取数)"]
    n_ok = 0
    for sid, code, state in results[:MAX_LINES]:
        n_ok += 1 if state == "ok" else 0
        lines.append("- [%s] %s %s" % (state, _clean(sid), code or "连接失败"))
    if len(results) > MAX_LINES:
        lines.append("- …(共 %d 个源, 已截断)" % len(results))
    lines.append("(小结: %d 个源, ok %d / dead %d —— dead 不用再花任何工具调用)"
                 % (len(results), n_ok, len(results) - n_ok))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="当日源入口确定性预探(注入取数 prompt)")
    ap.add_argument("--date", default=biz_today(), help="业务日期 YYYY-MM-DD(默认东八区今天)")
    ap.add_argument("--group", type=int, default=None,
                    help="只探该块(≥1, 与 scan_targets 分块同口径); 缺省=全部")
    args = ap.parse_args()
    try:
        out = render_probe(args.date, args.group)
    except (OSError, ValueError) as exc:
        print("[通道探测][WARN] 预探失败(%s), 退回自行探测" % _clean(exc), file=sys.stderr)
        return 0
    if out:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
