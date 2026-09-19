#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成每日情报日报(Markdown + Excel/CSV)。

数据来自 SQLite 事件库, 模板为 templates/daily_report.md(占位符渲染)。
默认渲染政策变化; --outbreak 切换为疫情兼容日报
(templates/policy_report.md, 只取 record_type=policy 的记录)。

用法:
  python report.py                      # 今天的政策监测日报(默认)
  python report.py --date 2026-09-12 --excel --docx
  python report.py --outbreak             # 疫情日报兼容模式

输出: data/reports/<date>-daily-report.md (+ .xlsx 或 .csv)
      data/reports/<date>-policy-report.md
"""
import argparse
import csv
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates"))
from base import auto_fit_columns, auto_fit_row_heights, style_data_row, style_header_row
from normalize import DATA_DIR, REPO, load_events, now, open_db
TEMPLATE = os.path.join(REPO, "templates", "daily_report.md")
POLICY_TEMPLATE = os.path.join(REPO, "templates", "policy_report.md")
FOCUS_ORDER = {"立即关注": 0, "持续观察": 1, "常规记录": 2}
RISK_ICON = {"high": "🔴", "medium": "🟡", "low": "🟢"}
TABLE_HEADER = ("| # | 病害 | 类别 | 国家/地区 | 发生日期 | 核验 | 对华风险 | 关注等级 | 来源 |\n"
                "|---|---|---|---|---|---|---|---|---|")
ACTION_ICON = {"收紧": "🔴", "放松": "🟢", "调整": "🟡", "恢复": "🔵"}
DOMAIN_LABEL = {"animal": "动物卫生", "plant": "植物保护", "both": "动植物",
                "trade": "进出口贸易", "measures": "口岸措施"}
POLICY_TABLE_HEADER = ("| # | 国家/地区 | 动作 | 政策领域 | 相关病害/商品 | 生效日期 | 政策状态 | 摘要 | 影响类型 | 对华影响 | 后续动作 | 来源 |\n"
                       "|---|---|---|---|---|---|---|---|---|---|---|---|")


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


def collect(date, days_back, db_path=None, record_type="policy"):
    """选取某期日报的数据: 当日新增 + 持续关注 + 全库(report_docx/push_report 复用)。

    record_type: outbreak(默认, 疫情事件) / policy(政策变化记录)。
    """
    con = open_db(db_path)
    all_events = load_events(con)
    con.close()
    if not all_events:
        return None
    all_events = [e for e in all_events
                  if (e.get("record_type") or "outbreak") == record_type]
    if not all_events:
        return None
    new = [e for e in all_events
           if e.get("verification_status") != "merged"
           and str(e.get("first_seen", ""))[:10] == date]
    lo, hi = _date(date) - datetime.timedelta(days=days_back), _date(date)
    new_ids = {e["event_id"] for e in new}
    if record_type == "policy":
        # 政策记录的"持续关注": 近窗内生效/发布的所有未合并记录, 按动作类型排序
        active = [e for e in all_events
                  if e.get("verification_status") != "merged"
                  and e["event_id"] not in new_ids
                  and e.get("event_date") and lo <= _date(e["event_date"]) <= hi]
    else:
        active = [e for e in all_events
                  if e.get("verification_status") != "merged"
                  and e["event_id"] not in new_ids
                  and (e.get("china_risk") or {}).get("focus") in ("立即关注", "持续观察")
                  and e.get("event_date") and lo <= _date(e["event_date"]) <= hi]
    new.sort(key=sort_key)
    active.sort(key=sort_key)
    return {"all": all_events, "new": new, "active": active,
            "covered": sorted(new + active, key=sort_key)}


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


def headline_items(new, active):
    """五问速览的纯文本条目(Markdown 与 Word 简报共用)。"""
    if not new and not active:
        return ["今日无新增疫情事件, 也无需要持续关注的事件。"]
    animals = sum(1 for e in new if e.get("category") == "animal")
    countries = sorted({e.get("country_cn") for e in new if e.get("country_cn")})
    diseases = sorted({e.get("disease_name_cn") for e in new if e.get("disease_name_cn")})
    risk = [(e.get("china_risk") or {}) for e in new]
    highs = [e for e in new + active
             if (e.get("china_risk") or {}).get("focus") == "立即关注"]
    return [
        "哪些疫情正在发生: 今日新增 %d 起(动物 %d 起 / 植物 %d 起), 另有 %d 起持续关注中。" %
        (len(new), animals, len(new) - animals, len(active)),
        "发生在哪里: 涉及 %d 个国家/地区: %s。" %
        (len(countries), "、".join(countries[:6]) + ("等" if len(countries) > 6 else "") if countries else "—"),
        "涉及动植物: %s。" % ("、".join(diseases[:6]) + ("等" if len(diseases) > 6 else "") if diseases else "—"),
        "是否可能影响我国: 今日新增中 high %d 起 / medium %d 起 / low %d 起 / 未研判 %d 起。" % (
            sum(1 for r in risk if r.get("level") == "high"),
            sum(1 for r in risk if r.get("level") == "medium"),
            sum(1 for r in risk if r.get("level") == "low"),
            sum(1 for r in risk if not r.get("level"))),
        "值得立即关注: %s" % (
            "; ".join("%s(%s·%s)" % (e.get("disease_name_cn"), e.get("country_cn"),
                                     (e.get("china_risk") or {}).get("level"))
                      for e in highs[:5]) if highs else "今日无。"),
    ]


def make_headline(new, active, date):
    return "\n".join("- " + s for s in headline_items(new, active))


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
        name = e.get("title_cn") or e.get("title_en") or e.get("disease_name_cn") or "（无标题）"
        if status in icons:
            lines.append("- %s %s @ %s(%s) 核验:%s: %s" % (
                icons[status], name, e.get("country_cn"),
                e["event_id"], status, e.get("summary_cn") or "见来源"))
        elif status == "verified" and not e.get("china_risk") and not e.get("impact_level"):
            lines.append("- ⏳ %s @ %s(%s) 已核验、待影响研判: %s" % (
                name, e.get("country_cn"), e["event_id"],
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


# ---------- 政策变化日报(--policy) ----------

POLICY_LEVEL_LABEL = {"high": "高影响", "medium": "中影响", "low": "低影响"}
POLICY_LEVEL_REVERSE = {v: k for k, v in POLICY_LEVEL_LABEL.items()}


def policy_impact_level(p):
    """政策对华影响等级(新字段优先,兼容旧 china_risk.level)。"""
    level = p.get("impact_level")
    if level in ("高影响", "中影响", "低影响"):
        return level
    return POLICY_LEVEL_LABEL.get((p.get("china_risk") or {}).get("level"))


def policy_title(p):
    return p.get("title_cn") or p.get("title_en") or p.get("summary_cn") or "（无标题）"


def policy_row(i, p):
    risk = p.get("china_risk") or {}
    domain = DOMAIN_LABEL.get(p.get("policy_domain"), p.get("policy_domain") or "-")
    products = "、".join(p.get("products") or []) or \
        ("、".join(filter(None, [p.get("disease_name_cn") or p.get("disease_name_en")])) or "-")
    icon = ACTION_ICON.get(p.get("action_type"), "⚪")
    return "| %d | %s%s | %s%s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
        i, p.get("country_cn") or "-", ("·" + str(p["region"])) if p.get("region") else "",
        icon, p.get("action_type") or "-", domain, products,
        p.get("effective_date") or p.get("event_date") or "-",
        p.get("policy_status") or "已生效", (policy_title(p) or "-")[:40],
        p.get("impact_type") or "未研判", p.get("impact_level") or (risk.get("level") or "未研判"),
        p.get("recommended_action") or "未注明", src_link(p.get("source")))


def make_policy_table(events):
    if not events:
        return "_（无）_\n"
    lines = [POLICY_TABLE_HEADER]
    lines += [policy_row(i, e) for i, e in enumerate(events, 1)]
    return "\n".join(lines) + "\n"


def policy_sort_key(p):
    order = {"收紧": 0, "调整": 1, "放松": 2, "恢复": 3}
    risk = p.get("china_risk") or {}
    return (order.get(p.get("action_type"), 4),
            FOCUS_ORDER.get(risk.get("focus"), 3), str(p.get("event_date")))


def policy_headline_items(new, active):
    if not new and not active:
        return ["本期无新增政策变化记录。"]
    covered = new + active
    tighten = sum(1 for p in covered if p.get("action_type") == "收紧")
    relax = sum(1 for p in covered if p.get("action_type") in ("放松", "恢复"))
    countries = sorted({p.get("country_cn") for p in new if p.get("country_cn")})
    domains = sorted({DOMAIN_LABEL.get(p.get("policy_domain"), p.get("policy_domain"))
                      for p in new if p.get("policy_domain")})
    highs = [p for p in covered
             if (p.get("china_risk") or {}).get("focus") == "立即关注"
             or p.get("action_type") == "收紧"
             or policy_impact_level(p) == "高影响"]
    unverified = sum(1 for p in covered if p.get("verification_status") == "unverified")
    return [
        "变化了多少: 本期新增 %d 条政策变化(收紧 %d / 放松或恢复 %d / 其他 %d), 另有 %d 条近期变动持续跟踪中。" % (
            len(new), tighten, relax, len(new) - tighten - relax, len(active)),
        "哪些国家: 涉及 %d 个国家/地区: %s。" % (
            len(countries), "、".join(countries[:8]) + ("等" if len(countries) > 8 else "") if countries else "—"),
        "涉及领域: %s。" % ("、".join(domains) if domains else "—"),
        "对华影响: 高影响 %d 条 / 中影响 %d 条 / 低影响 %d 条 / 未研判 %d 条。" % (
            sum(1 for p in covered if policy_impact_level(p) == "高影响"),
            sum(1 for p in covered if policy_impact_level(p) == "中影响"),
            sum(1 for p in covered if policy_impact_level(p) == "低影响"),
            sum(1 for p in covered if policy_impact_level(p) is None)),
        "值得立即关注: %s" % (
            "; ".join("%s(%s·%s)" % (policy_title(p)[:24], p.get("country_cn"),
                                     p.get("action_type")) for p in highs[:5]) if highs else "本期无。"),
    ] + (["待核实: %d 条尚未完成官方出处核验, 见『待核实信息』栏。" % unverified] if unverified else [])


def make_policy_headline(new, active):
    return "\n".join("- " + s for s in policy_headline_items(new, active))


def make_policy_detail(events):
    """收紧/调整/高影响动作的详情块。"""
    keys = [p for p in events if p.get("action_type") in ("收紧", "调整")
            or policy_impact_level(p) == "高影响"]
    if not keys:
        return "_本期无收紧、调整或高影响类政策变化。_\n"
    blocks = []
    for i, p in enumerate(keys, 1):
        risk = p.get("china_risk") or {}
        links = [src_link(p.get("source"))] + \
            [src_link(s) for s in p.get("cross_sources", []) if isinstance(s, dict)]
        blocks.append(
            "### %d. %s · %s(%s)\n"
            "- **动作**: %s → %s | **政策状态**: %s\n"
            "- **摘要**: %s\n"
            "- **相关病害/商品**: %s\n"
            "- **生效日期**: %s | **有效期末**: %s\n"
            "- **对华影响**: %s%s(%s)— %s\n"
            "- **影响类型/中国关联**: %s / %s\n"
            "- **建议动作**: %s\n"
            "- **依据/原文**: %s\n"
            "- **来源**: %s\n" % (
                i, p.get("country_cn"), policy_title(p), p["event_id"],
                p.get("prev_action") or "（此前无记录）", p.get("action_type") or "-",
                p.get("policy_status") or "已生效",
                p.get("summary_cn") or "见来源",
                "、".join(filter(None, [p.get("disease_name_cn") or p.get("disease_name_en")])) or
                ("、".join(p.get("products") or []) if p.get("products") else "背景资料未提及"),
                p.get("event_date"), p.get("effective_until") or "未注明",
                RISK_ICON.get(POLICY_LEVEL_REVERSE.get(policy_impact_level(p)), ""),
                policy_impact_level(p) or "未研判",
                risk.get("score") if risk.get("score") is not None else "-",
                risk.get("rationale") or "待研判",
                p.get("impact_type") or "未研判", p.get("china_relevance") or "未研判",
                p.get("recommended_action") or "待研判",
                p.get("legal_basis") or "见来源原文",
                " | ".join(links)))
    return "\n".join(blocks)


def make_policy_impact_summary(events):
    judged = [p for p in events if p.get("china_risk") or p.get("impact_level")]
    if not judged:
        return "_本期暂无已完成对华影响研判的政策变化。_\n"
    lines = []
    for p in sorted(judged, key=lambda x: -((x.get("china_risk") or {}).get("score") or 0)):
        r = p.get("china_risk") or {}
        lines.append("- **%s(%s)** %s·%s(%s)·%s: %s" % (
            policy_title(p)[:36], p.get("country_cn"),
            p.get("impact_type") or "未分类", p.get("impact_level") or r.get("level") or "未研判",
            p.get("china_relevance") or "未研判",
            (r.get("score") if r.get("score") is not None else "-"),
            r.get("rationale") or p.get("recommended_action") or ""))
    return "\n".join(lines) + "\n"


def make_policy_watchlist_diff(events):
    """提示政策涉及的病害、商品和后续跟踪对象。"""
    lines = []
    for p in events:
        dis = p.get("disease_name_cn") or p.get("disease_name_en")
        if not dis:
            continue
        lines.append("- %s(%s) %s — %s: %s" % (
            dis, p.get("country_cn"), p.get("action_type") or "-",
            policy_impact_level(p) or "未研判", p.get("summary_cn") or policy_title(p)[:40]))
    return "\n".join(lines) + "\n" if lines else "_本期无记录明确关联病害或商品的政策变化。_\n"


def export_table(base_path, events, record_type="policy"):
    def val(value):
        return "未注明" if value in (None, "", [], {}) else value

    if record_type == "policy":
        headers = ["记录ID", "政策标题", "英文标题", "动作类型", "政策领域", "发布国家/地区", "发布机构",
                   "涉及国家/地区", "受影响商品", "关联病害", "适用范围", "发布日期", "生效日期", "有效期",
                   "政策状态", "公告/法规编号", "核验状态", "影响类型", "影响等级", "中国关联", "建议动作",
                   "影响分", "关注等级", "影响依据", "原文摘要", "来源名称", "来源URL", "来源层级", "抓取时间"]
        rows = [[val(e.get("event_id")), val(e.get("title_cn")), val(e.get("title_en")),
                 val(e.get("action_type")), val(e.get("policy_domain")), val(e.get("country_cn")),
                 val(e.get("issuer_cn") or e.get("issuer_en")),
                 "、".join(e.get("target_countries") or []) or val(None),
                 "、".join(e.get("products") or []) or val(None),
                 val(e.get("disease_name_cn") or e.get("disease_name_en")), val(e.get("scope")),
                 val(e.get("published_date") or e.get("report_date") or (e.get("source") or {}).get("publish_date")),
                 val(e.get("effective_date") or e.get("event_date")), val(e.get("effective_until")),
                 val(e.get("policy_status") or "已生效"), val(e.get("legal_basis")), val(e.get("verification_status")),
                 val(e.get("impact_type")), val(e.get("impact_level") or policy_impact_level(e)),
                 val(e.get("china_relevance")), val(e.get("recommended_action")),
                 val((e.get("china_risk") or {}).get("score")),
                 val((e.get("china_risk") or {}).get("focus")), val((e.get("china_risk") or {}).get("rationale")),
                 val(e.get("summary_cn")), val((e.get("source") or {}).get("name")),
                 val((e.get("source") or {}).get("url")), val((e.get("source") or {}).get("tier")),
                 val(e.get("first_seen"))] for e in events]
        sheet = "政策变化台账"
    else:
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
        sheet = "疫情事件"
    try:
        from openpyxl import Workbook
        from openpyxl.formatting.rule import FormulaRule
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
        wb = Workbook()
        wb.properties.title = "全球动植物检疫政策监测台账" if record_type == "policy" else "全球动植物疫情事件台账"
        wb.properties.creator = "疫见全球"
        ws = wb.active
        ws.title = sheet
        ws.freeze_panes = "A2"
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.page_setup.orientation = "landscape"
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
        ws.print_title_rows = "1:1"
        ws.page_margins.left = 0.25
        ws.page_margins.right = 0.25
        ws.page_margins.top = 0.5
        ws.page_margins.bottom = 0.5
        ws.append(headers)
        for r in rows:
            ws.append(r)
        style_header_row(ws, 1, 1, len(headers))
        for idx in range(len(rows)):
            style_data_row(ws, idx + 2, 1, len(headers), idx)
        auto_fit_columns(ws, min_width=10, max_width=34)
        auto_fit_row_heights(ws, header_row=1, data_start_row=2)
        ws.auto_filter.ref = "A1:%s%d" % (get_column_letter(len(headers)), len(rows) + 1)
        if rows:
            ws.conditional_formatting.add("A2:%s%d" % (get_column_letter(len(headers)), len(rows) + 1),
                FormulaRule(formula=['MOD(ROW(),2)=0'], fill=PatternFill("solid", fgColor="F7F7F5")))
        if record_type == "policy":
            status_col = headers.index("核验状态") + 1
            action_col = headers.index("动作类型") + 1
            for r in range(2, len(rows) + 2):
                ws.cell(r, status_col).alignment = Alignment(horizontal="center", vertical="top", wrap_text=True)
                ws.cell(r, action_col).alignment = Alignment(horizontal="center", vertical="top", wrap_text=True)
        path = base_path + ".xlsx"
        wb.save(path)
        return path
    except ImportError:
        path = base_path + ".csv"
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerows([headers] + rows)
        return path


def main():
    ap = argparse.ArgumentParser(description="生成政策监测日报(默认)或疫情日报(兼容模式)")
    ap.add_argument("--date", default=datetime.date.today().isoformat(), help="日报日期, 默认今天")
    ap.add_argument("--days-back", type=int, default=14, help="近期政策/事件的回看窗口(天)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--policy", action="store_true", help="政策监测日报(默认)")
    mode.add_argument("--outbreak", action="store_true", help="疫情日报兼容模式")
    ap.add_argument("--excel", action="store_true", help="同时导出 Excel(无 openpyxl 时降级 CSV)")
    ap.add_argument("--docx", action="store_true", help="同时生成 Word 简报(无 python-docx 时跳过)")
    ap.add_argument("--db-path", help="SQLite 路径覆盖")
    args = ap.parse_args()

    rtype = "outbreak" if args.outbreak else "policy"
    policy_mode = rtype == "policy"
    data = collect(args.date, args.days_back, args.db_path, record_type=rtype)
    if data is None:
        if not policy_mode:
            print("[提示] 事件库为空或无疫情记录。")
            return 1
        # 政策模式即使当天/当前库暂无政策，也要生成可交付的空台账，避免网页按钮误报失败。
        data = {"all": [], "new": [], "active": [], "covered": []}
        print("[提示] 当前没有政策记录, 仍生成空的政策监测日报。")
    new, active, covered = data["new"], data["active"], data["covered"]

    if policy_mode:
        replacements = {
            "{{date}}": args.date,
            "{{generated_at}}": now(),
            "{{days_back}}": str(args.days_back),
            "{{headline}}": make_policy_headline(new, active),
            "{{new_table}}": make_policy_table(new),
            "{{active_table}}": make_policy_table(active),
            "{{focus_detail}}": make_policy_detail(covered),
            "{{impact_summary}}": make_policy_impact_summary(covered),
            "{{watchlist_diff}}": make_policy_watchlist_diff(covered),
            "{{unverified}}": make_open_items(covered),
            "{{stats}}": make_stats(data["all"], new, active),
            "{{sources}}": make_sources(covered),
        }
        template_path = POLICY_TEMPLATE
        base = os.path.join(DATA_DIR, "reports", "%s-policy-report" % args.date)
    else:
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
            "{{stats}}": make_stats(data["all"], new, active),
            "{{sources}}": make_sources(covered),
        }
        template_path = TEMPLATE
        base = os.path.join(DATA_DIR, "reports", "%s-daily-report" % args.date)

    with open(template_path, encoding="utf-8") as f:
        report_md = f.read()
    for k, v in replacements.items():
        report_md = report_md.replace(k, v)
    leftover = [k for k in replacements if k in report_md]
    if leftover:
        print("[警告] 模板占位符未替换: %s" % leftover)

    out_dir = os.path.dirname(base)
    os.makedirs(out_dir, exist_ok=True)
    md_path = base + ".md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(report_md)

    print("[OK] %s" % md_path)
    if args.excel:
        print("[OK] %s" % export_table(base, covered, record_type=rtype))
    if args.docx:
        try:
            import report_docx
            print("[OK] %s" % report_docx.build(args.date, data, record_type=rtype))
        except ImportError:
            print("[提示] 未安装 python-docx, 跳过 Word 简报(pip install python-docx)")
    print("\n本期: 新增 %d 条, 近期变动 %d 条(%s库共 %d 条)。" % (
        len(new), len(active), "政策变化" if policy_mode else "疫情事件", len(data["all"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
