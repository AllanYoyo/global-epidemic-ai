#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""疫见全球 · 死源分诊表(2026-10-30 引入)—— 把「N 个源死了」变成「谁死、为什么死、该干什么」

背景(2026-10-30 生产实测): 当日 47 源里 17 个探活即死, 但生产**不记死因** ——
探活拿到的 HTTP 码与降级 note 当场被丢掉, search-log 只写一行「预探 dead」。
于是「17 个死源」既分不出死因、也定位不到该改哪个参数/哪个 URL, 只能一次次人工
重跑 curl 分诊。本脚本把这件事日常化: 读近 N 日的 fetch-status.json, 出一张
**逐源 × 逐日**的分诊表。

口径:
  · 死 = 探活即死(没进抓取);  抓取中失败 = 另一类(源活着但抓取报错), 单独列;
  · 零产出 = 探活活、但当日一条没存出来(抓到了却没可用条目 —— 假活);
  · 死因分类 A/B/C/D 见 probe_channels.classify_dead_cause:
      A 参数可救(换参数/换入口能救) / B 入口配置错(URL 错了) /
      C 反爬封锁(403, curl 无解) / D 源已失效(DNS 不解析/连不上)。

用法:
  python scripts/dead_sources.py                    # 近 7 日
  python scripts/dead_sources.py --days 30
  python scripts/dead_sources.py --date 2026-10-30  # 指定某天的明细
退出码: 0 = 正常(有死源不算错误); 2 = 参数/环境错误。
"""
import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from normalize import biz_today  # 业务时区口径一致

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("EPIDEMIC_DATA_DIR") or os.path.join(REPO, "data")

# 死因分类 → 该干什么(分诊建议)
ADVICE = {
    "A-参数可救": "改降级链参数(-k/-4/超时)或换入口(alt_urls)—— 本周可救",
    "B-入口配置错": "改 config/sources.yaml 的 url/alt_urls —— 配置活, 立刻可救",
    "C-反爬封锁": "curl 无解: 走替代上游(WTO ePing / 省级 TBT 站)或换官方入口",
    "D-源已失效": "域名/站点没了: 重新定位官方域名, 或降级出每日必扫清单",
}


def _load_day(raw_dir, day):
    """读一天的 fetch-status.json(没有就返回 None)。"""
    path = os.path.join(raw_dir, day, "fetch-status.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _days_with_status(raw_dir, days):
    """最近 N 天里有 fetch-status.json 的日子(新→旧)。"""
    out = []
    try:
        avail = sorted(d for d in os.listdir(raw_dir) if os.path.isdir(os.path.join(raw_dir, d)))
    except OSError:
        return out
    for day in reversed(avail[-days:]):
        if _load_day(raw_dir, day) is not None:
            out.append(day)
    return out


def report(raw_dir, days, focus_day=None):
    # days 全程保持 int(回看天数); 有状态文件的日子单存一个 list —— 2026-10-30 审查:
    # 原实现把参数直接覆盖成 list, 空数据提示行的 `%d` 格式化 list 直接 TypeError 裸崩
    # (退出码 1 + traceback), 而这恰是新部署/取数段从未跑过时的第一路径。
    days_with_status = _days_with_status(raw_dir, days)
    if not days_with_status:
        print("[提示] 近 %d 天里没有 fetch-status.json(取数段还没跑过?): %s"
              % (days, os.path.join(raw_dir, "<日期>")))
        return 0

    per_day = {d: _load_day(raw_dir, d) for d in days_with_status}
    ordered = list(reversed(days_with_status))          # 旧→新

    print("=" * 78)
    print("一、逐日覆盖(存活源 = 探活活 + 抓取成功; 死源 = 探活即死)")
    print("%-12s %6s %6s %6s %8s %7s" % ("日期", "总数", "存活", "死", "存档", "全挂"))
    for d in ordered:
        s = per_day[d]
        print("%-12s %6s %6s %6s %8s %7s" % (
            d, s.get("sources_total"), s.get("sources_alive"), s.get("sources_dead"),
            s.get("archives"), "是" if s.get("infra_failure") else "否"))

    # ---- 逐源死因(探活即死) ----
    dead_by_sid = {}
    for d in ordered:
        for det in per_day[d].get("sources_dead_detail") or []:
            sid = det.get("source_id")
            dead_by_sid.setdefault(sid, []).append((d, det))

    print()
    print("=" * 78)
    print("二、逐源死因(探活即死的源)")
    if not dead_by_sid:
        print("无死源。")
        print("(注: 改造前的历史 fetch-status.json 不带 sources_dead_detail —— "
              "死因留痕是 2026-10-30 才加的, 历史日只能看第一段逐日覆盖。)")
    else:
        print("%-22s %5s %5s %-14s %s" % ("源id", "死天", "连死", "死因分类", "最后状态/建议"))
        for sid in sorted(dead_by_sid):
            rows = dead_by_sid[sid]
            last_day, det = rows[-1]
            cause = det.get("cause") or "?"
            code = det.get("probe_code")
            note = det.get("probe_note") or ""
            # 连续死天数(从最近一天往回数)
            streak = 0
            for d in ordered:
                if any(r[0] == d for r in rows):
                    streak += 1
                else:
                    break
            state = "%s %s" % (code if code else "连接失败", note)
            print("%-22s %5d %5d %-14s %s" % (sid, len(rows), streak, cause, state[:30]))
            print("%-22s %s" % ("", ADVICE.get(cause, "")))

    # ---- 假活: 探活活但一条没存出来 ----
    zero = {}
    for d in ordered:
        for ps in per_day[d].get("per_source") or []:
            if not ps.get("archives"):
                zero.setdefault(ps.get("source_id"), []).append(d)
    print()
    print("=" * 78)
    print("三、假活(探活活, 但当日一条没存出来 —— 抓到页面却抽不出条目)")
    if not zero:
        print("无。")
    else:
        print("%-22s %s" % ("源id", "零产出天数"))
        for sid in sorted(zero, key=lambda s: -len(zero[s])):
            print("%-22s %d 天" % (sid, len(zero[sid])))
        print("提示: 假活源多数是「入口页选错」(抓到首页而不是新闻列表页)或"
              "「列表页不带日期线索」—— 与死源是两回事, 但同样吃覆盖。")

    if focus_day:
        s = per_day.get(focus_day)
        if s:
            print()
            print("=" * 78)
            print("四、%s 明细" % focus_day)
            for det in s.get("sources_dead_detail") or []:
                print("  死 %-20s code=%-4s note=%-28s %s" % (
                    det.get("source_id"), det.get("probe_code"),
                    (det.get("probe_note") or "")[:28], det.get("cause")))
            for ps in s.get("per_source") or []:
                print("  活 %-20s status=%-10s archives=%-3s entry=%s" % (
                    ps.get("source_id"), ps.get("status"), ps.get("archives"),
                    (ps.get("entry_url") or "-")))
    return 0


def main():
    ap = argparse.ArgumentParser(description="死源分诊表(逐源 × 逐日)")
    ap.add_argument("--date", default=None, help="业务日期 YYYY-MM-DD(指定某天明细; 缺省=今天)")
    ap.add_argument("--days", type=int, default=7, help="回看天数(默认 7)")
    ap.add_argument("--data-dir", default=None, help="数据目录(缺省=$EPIDEMIC_DATA_DIR 或仓库 data/)")
    args = ap.parse_args()

    raw_dir = os.path.join(args.data_dir or DATA_DIR, "raw")
    if not os.path.isdir(raw_dir):
        print("[错误] 数据目录不存在: %s" % raw_dir)
        return 2
    try:
        return report(raw_dir, args.days, args.date)
    except (OSError, ValueError) as exc:
        print("[错误] 分诊失败: %s" % exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
