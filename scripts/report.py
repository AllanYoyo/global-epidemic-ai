#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成每日疫情情报日报(Markdown + Excel/CSV)。

数据来自 SQLite 事件库, 模板为 templates/daily_report.md(占位符渲染)。

用法:
  python report.py                      # 今天的日报
  python report.py --date 2026-09-12 --excel

输出: data/reports/<date>-daily-report.md (+ .xlsx 或 .csv)
"""
import argparse
import csv
import datetime
import json
import os

from normalize import DATA_DIR, REPO, load_events, now, open_db

TEMPLATE = os.path.join(REPO, "templates", "daily_report.md")
FOCUS_ORDER = {"立即关注": 0, "持续观察": 1, "常规记录": 2}
RISK_ICON = {"high": "🔴", "medium": "🟡", "low": "🟢"}
TABLE_HEADER = ("| # | 病害 | 类别 | 国家/地区 | 发生日期 | 核验 | 对华风险 | 关注等级 | 来源 |\n"
                "|---|---|---|---|---|---|---|---|---|")


def _date(s):
    return datetime.date.fromisoformat(str(s)[:10])


def src_link(s):
    if not isinstance(s, dict):
        return "-"
    name = s.get("name") or "来源"
    url = s.get("url")
    return "[%s](%s)" % (name, url) if url else name


def sort_key(e):
    risk = e.get("china_risk") or {}
    return (FOCUS_ORDER.get(risk.get("focus"), 3), -(risk.get("score") or 0), str(e.get("event_date")))


def event_row(i, e):
    risk = e.get("china_risk") or {}
    place = str(e.get("country_cn") or "-") + (("·" + str(e["region"])) if e.get("region") else "")
    icon = RISK_ICON.get(risk.get("level"), "⚪")
    return "| %d | %s(%s) | %s | %s | %s | %s | %s%s | %s | %s |" % (
        i, e.get("disease_name_cn"), e.get("disease_name_en"), e.get("category"),
        place, e.get("event_date"), e.get("verification_status"),
        icon, risk.get("level", "-"), risk.get("focus", "-"), src_link(e.get("source")))


def make_table(events):
    if not events:
        return "_（无）_\n"
    lines = [TABLE_HEADER]
    lines += [event_row(i, e) for i, e in enumerate(events, 1)]
    return "\n".join(lines) + "\n"


def make_headline(new, active, date):
    if not new and not active:
        return "- 今日无新增疫情事件, 也无需要持续关注的事件。"
    animals = sum(1 for e in new if e.get("category") == "animal")
    countries = sorted({e.get("country_cn") for e in new if e.get("country_cn")})
    diseases = sorted({e.get("disease_name_cn") for e in new if e.get("disease_name_cn")})
    risk = [(e.get("china_risk") or {}) for e in new]
    highs = [e for e in new + active
             if (e.get("china_risk") or {}).get("focus") == "立即关注"]
    lines = [
        "- **哪些疫情正在发生**: 今日新增 %d 起(动物 %d 起 / 植物 %d 起), 另有 %d 起持续关注中。" %
        (len(new), animals, len(new) - animals, len(active)),
        "- **发生在哪里**: 涉及 %d 个国家/地区: %s。" %
        (len(countries), "、".join(countries[:6]) + ("等" if len(countries) > 6 else "") if countries else "—"),
        "- **涉及动植物**: %s。" % ("、".join(diseases[:6]) + ("等" if len(diseases) > 6 else "") if diseases else "—"),
        "- **是否可能影响我国**: 今日新增中 high %d 起 / medium %d 起 / low %d 起 / 未研判 %d 起。" % (
            sum(1 for r in risk if r.get("level") == "high"),
            sum(1 for r in risk if r.get("level") == "medium"),
            sum(1 for r in risk if r.get("level") == "low"),
            sum(1 for r in risk if not r.get("level"))),
        "- **值得立即关注**: %s" % (
            "; ".join("%s(%s·%s)" % (e.get("disease_name_cn"), e.get("country_cn"),
                                     (e.get("china_risk") or {}).get("level"))
                      for e in highs[:5]) if highs else "今日无。"),
    ]
    return "\n".join(lines)


def make_focus_detail(events):
    highs = [e for e in events if (e.get("china_risk") or {}).get("focus") == "立即关注"]
    if not highs:
        return "_今日无“立即关注”级别事件。_\n"
    blocks = []
    for i, e in enumerate(highs, 1):
        risk = e.get("china_risk") or {}
        links = [src_link(e.get("source"))] + \
            [src_link(s) for s in e.get("cross_sources", []) if isinstance(s, dict)]
        blocks.append(
            "### %d. %s · %s(%s)\n"
            "- **摘要**: %s\n"
            "- **对华风险**: %s %s(%s)— %s\n"
            "- **贸易关联**: %s  \n  **现有措施**: %s\n"
            "- **来源**: %s\n" % (
                i, e.get("country_cn"), e.get("disease_name_cn"), e["event_id"],
                e.get("summary_cn") or "见来源",
                RISK_ICON.get(risk.get("level"), ""), risk.get("level"), risk.get("score"),
                risk.get("rationale") or "-",
                risk.get("trade_relevance") or "背景资料未提及",
                risk.get("existing_gacc_measures") or "背景资料未提及",
                " | ".join(links)))
    return "\n".join(blocks)


def make_risk_summary(events):
    judged = [e for e in events if e.get("china_risk")]
    if not judged:
        return "_本期暂无已完成对华风险研判的事件。_\n"
    lines = []
    for e in sorted(judged, key=lambda x: -((x.get("china_risk") or {}).get("score") or 0)):
        r = e["china_risk"]
        lines.append("- **%s(%s)** %s(%s): %s" % (
            e.get("disease_name_cn"), e.get("country_cn"), r.get("level"), r.get("score"),
            r.get("rationale") or ""))
    return "\n".join(lines) + "\n"


def make_open_items(events):
    """待办清单: 未核验 / 假消息嫌疑 / 已核验但未研判, 供人工复核。"""
    icons = {"unverified": "❓", "false_positive": "⚠️"}
    lines = []
    for e in events:
        status = e.get("verification_status")
        if status in icons:
            lines.append("- %s %s @ %s(%s) 核验:%s: %s" % (
                icons[status], e.get("disease_name_cn"), e.get("country_cn"),
                e["event_id"], status, e.get("summary_cn") or "见来源"))
        elif status == "verified" and not e.get("china_risk"):
            lines.append("- ⏳ %s @ %s(%s) 已核验、待风险研判: %s" % (
                e.get("disease_name_cn"), e.get("country_cn"), e["event_id"],
                e.get("summary_cn") or "见来源"))
    return "\n".join(lines) + "\n" if lines else "_无待核实/待研判事件。_\n"


def make_stats(all_events, new, active):
    by_status = {}
    for e in all_events:
        by_status[e.get("verification_status")] = by_status.get(e.get("verification_status"), 0) + 1
    detail = " / ".join("%s %d" % (k, v) for k, v in sorted(by_status.items(),
                                                          key=lambda kv: -kv[1]))
    return ("- 事件库累计: %d 条(%s)\n- 本期覆盖: 新增 %d 起, 持续关注 %d 起\n" %
            (len(all_events), detail, len(new), len(active)))


def make_sources(events):
    seen, items = {}, []
    for e in events:
        for s in [e.get("source")] + list(e.get("cross_sources") or []):
            if isinstance(s, dict) and s.get("url") and s["url"] not in seen:
                seen[s["url"]] = e["event_id"]
                items.append("- [%s](%s) — 事件 %s" % (s.get("name") or s["url"], s["url"], e["event_id"]))
    return "\n".join(items) + "\n" if items else "_（本期无来源记录）_\n"


def export_table(base_path, events):
    headers = ["event_id", "病害中文", "病害英文", "类别", "病原", "宿主/作物", "国家", "地区",
               "发生日期", "数量", "核验状态", "对华风险等级", "风险分", "关注等级",
               "风险依据", "一句话摘要", "来源名称", "来源URL"]
    rows = [[e.get("event_id"), e.get("disease_name_cn"), e.get("disease_name_en"),
             e.get("category"), e.get("pathogen"), ", ".join(e.get("host_species") or []),
             e.get("country_cn"), e.get("region"), e.get("event_date"),
             json.dumps(e.get("quantity") or {}, ensure_ascii=False),
             e.get("verification_status"),
             (e.get("china_risk") or {}).get("level"),
             (e.get("china_risk") or {}).get("score"),
             (e.get("china_risk") or {}).get("focus"),
             (e.get("china_risk") or {}).get("rationale"),
             e.get("summary_cn"),
             (e.get("source") or {}).get("name"), (e.get("source") or {}).get("url")]
            for e in events]
    try:
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "疫情事件"
        ws.append(headers)
        for r in rows:
            ws.append(r)
        path = base_path + ".xlsx"
        wb.save(path)
        return path
    except ImportError:
        path = base_path + ".csv"
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerows([headers] + rows)
        return path


def main():
    ap = argparse.ArgumentParser(description="生成每日疫情情报日报")
    ap.add_argument("--date", default=datetime.date.today().isoformat(), help="日报日期, 默认今天")
    ap.add_argument("--days-back", type=int, default=14, help="持续关注事件的回看窗口(天)")
    ap.add_argument("--excel", action="store_true", help="同时导出 Excel(无 openpyxl 时降级 CSV)")
    ap.add_argument("--db-path", help="SQLite 路径覆盖")
    args = ap.parse_args()

    con = open_db(args.db_path)
    all_events = load_events(con)
    if not all_events:
        print("[提示] 事件库为空: 先运行 normalize.py 入库事件。")
        return 1

    new = [e for e in all_events
           if e.get("verification_status") != "merged"
           and str(e.get("first_seen", ""))[:10] == args.date]
    lo, hi = _date(args.date) - datetime.timedelta(days=args.days_back), _date(args.date)
    new_ids = {e["event_id"] for e in new}
    active = [e for e in all_events
              if e.get("verification_status") != "merged"
              and e["event_id"] not in new_ids
              and (e.get("china_risk") or {}).get("focus") in ("立即关注", "持续观察")
              and e.get("event_date") and lo <= _date(e["event_date"]) <= hi]
    new.sort(key=sort_key)
    active.sort(key=sort_key)
    covered = sorted(new + active, key=sort_key)

    replacements = {
        "{{date}}": args.date,
        "{{generated_at}}": now(),
        "{{run_id}}": args.date,
        "{{days_back}}": str(args.days_back),
        "{{headline}}": make_headline(new, active, args.date),
        "{{new_table}}": make_table(new),
        "{{active_table}}": make_table(active),
        "{{focus_detail}}": make_focus_detail(covered),
        "{{risk_summary}}": make_risk_summary(covered),
        "{{unverified}}": make_open_items(covered),
        "{{stats}}": make_stats(all_events, new, active),
        "{{sources}}": make_sources(covered),
    }
    with open(TEMPLATE, encoding="utf-8") as f:
        report = f.read()
    for k, v in replacements.items():
        report = report.replace(k, v)
    leftover = [k for k in replacements if k in report]
    if leftover:
        print("[警告] 模板占位符未替换: %s" % leftover)

    out_dir = os.path.join(DATA_DIR, "reports")
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.join(out_dir, "%s-daily-report" % args.date)
    md_path = base + ".md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(report)

    print("[OK] %s" % md_path)
    if args.excel:
        print("[OK] %s" % export_table(base, covered))
    print("\n本期: 新增 %d 起, 持续关注 %d 起(事件库共 %d 条)。" %
          (len(new), len(active), len(all_events)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
