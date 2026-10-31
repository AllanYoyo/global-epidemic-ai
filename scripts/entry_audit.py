#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""疫见全球 · 源入口验收闸门(2026-10-31 引入)—— 防「拿网站首页应付」再次入库。

背景(2026-10-31 生产实测 + 用户逐站人工核查): 47 源里 30 个探活活, 但当日
28 个零候选 —— 入口大量是门户首页(链接几百条全是导航, 0 条带窗内日期),
「泛泛而谈的内容冲淡政策监测目的」。探活只判「网站死没死」, 判不了「入口
对不对」。本脚本把入口质量变成可验收的确定判级: 以后任何 sources.yaml 的
url/alt_urls 改动, 先过本脚本 A 级才准进。

判级(对入口页跑与取数管线**完全同款**的 extract_links → pick_candidates →
文章页验日期, 不另起一套口径 —— 两套口径是本仓库反复出事的病根):
  A = 窗内候选 ≥1(列表页直接带日期) 或 文章页验出窗内日期 ≥1 —— 可作政策入口
  B = 有文章型链接但窗内 0 日期 —— 内容不在 7 天窗/日期格式读不出, 人工复核
  C = 链接 <5 或抽不出文章型链接 —— 首页/JS 站, 禁止作政策入口
  X = 抓取失败(HTTP 非 2xx/3xx 或连接异常) —— 死口

用法:
  python scripts/entry_audit.py --url https://example.org/news
  python scripts/entry_audit.py --batch /tmp/cands.json      # {"sid": "url", ...}
  python scripts/entry_audit.py --current                    # 审计当日 sources.yaml 全部入口(找假活)
  python scripts/entry_audit.py --current --date 2026-10-31
退出码: 0=跑完; 2=参数/环境错误; 3=跑完但存在 C/X 级入口(--current 批量时)。
"""
import argparse
import datetime
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scan_targets
import fetch_pages

ENTRY_TIMEOUT = 20        # 入口页单次超时(秒), 比取数口径略宽(验收允许慢一次; 12s 会误杀 gov.uk 型慢站)
ARTICLE_TIMEOUT = 10
ARTICLES_TO_CHECK = 2     # 每入口最多验 2 篇文章页(与管线 verify per_source_cap 同款)
WORKERS = 6
MIN_LINKS = 5             # 少于这个数 = 首页壳/JS 站


def audit_one(url, today):
    """验一个入口, 返回 dict(grade/links/confirmed/to_verify/verified/why)。"""
    out = {"url": url, "grade": "X", "links": 0, "confirmed": 0,
           "to_verify": 0, "verified": 0, "why": ""}
    try:
        code, html = fetch_pages.curl(url, timeout=ENTRY_TIMEOUT)
    except Exception as exc:
        out["why"] = "抓取异常(%s)" % str(exc)[:50]
        return out
    if code < 200 or code >= 400:
        out["why"] = "HTTP %s" % code
        return out
    links = fetch_pages.extract_links(html or "", url)
    confirmed, to_verify = fetch_pages.pick_candidates(links, url, today)
    out.update(links=len(links), confirmed=len(confirmed), to_verify=len(to_verify))
    verified = 0
    for href, _anchor in to_verify[:ARTICLES_TO_CHECK]:
        try:
            c2, body = fetch_pages.curl(href, timeout=ARTICLE_TIMEOUT)
        except Exception:
            continue
        if 200 <= c2 < 400 and body:
            d = fetch_pages.page_publish_date(body)
            if d and fetch_pages.in_window(d, today):
                verified += 1
    out["verified"] = verified
    if confirmed or verified:
        out["grade"] = "A"
        out["why"] = "窗内候选 %d + 文章页验出 %d" % (len(confirmed), verified)
    elif len(links) < MIN_LINKS or not to_verify:
        out["grade"] = "C"
        out["why"] = "链接 %d 条且抽不出可验文章(首页壳/JS 站/全是导航)" % len(links)
    else:
        out["grade"] = "B"
        out["why"] = "文章型链接 %d 条但窗内 0 日期(内容不在 7 天窗或日期格式读不出)" % len(to_verify)
    return out


def run_batch(items, today):
    """并发验一批 (sid, url), 返回 [(sid, result)]。"""
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        return list(ex.map(lambda kv: (kv[0], audit_one(kv[1], today)), items))


def _print_table(rows):
    order = {"A": 0, "B": 1, "C": 2, "X": 3}
    rows = sorted(rows, key=lambda r: order.get(r[1]["grade"], 9))
    print("%-3s %-20s %7s %9s %9s %8s  %s"
          % ("级", "源", "链接", "窗内日期", "文章验证", "验出窗内", ""))
    for sid, r in rows:
        print("%-3s %-20s %7d %9d %9d %8d  %s"
              % (r["grade"], sid, r["links"], r["confirmed"], r["to_verify"],
                 r["verified"], r["url"]))
        if r["why"]:
            print("%-3s %-20s %s" % ("", "", "     ↳ %s" % r["why"]))
    grades = {}
    for _sid, r in rows:
        grades[r["grade"]] = grades.get(r["grade"], 0) + 1
    print("(小结: 共 %d 个 —— %s)" % (len(rows),
          " ".join("%s=%d" % (g, grades[g]) for g in sorted(grades))))


def main():
    ap = argparse.ArgumentParser(description="源入口验收闸门(判级 A/B/C/X)")
    ap.add_argument("--url", default=None, help="验单个候选入口 URL")
    ap.add_argument("--batch", default=None, help="批量验: JSON 文件 {sid: url}")
    ap.add_argument("--current", action="store_true",
                    help="审计当日 sources.yaml 全部入口(探活活着的才验得出, 死口记 X)")
    ap.add_argument("--date", default=None, help="业务日期 YYYY-MM-DD(默认东八区今天)")
    args = ap.parse_args()
    modes = [m for m, v in (("url", args.url), ("batch", args.batch), ("current", args.current)) if v]
    if len(modes) != 1:
        print("[错误] --url / --batch / --current 三选一")
        return 2
    today = datetime.date.fromisoformat(args.date) if args.date else (
        datetime.datetime.utcnow() + datetime.timedelta(hours=8)).date()

    if args.url:
        _print_table([("候选", audit_one(args.url, today))])
        return 0

    if args.batch:
        try:
            with open(args.batch, encoding="utf-8") as f:
                cands = json.load(f)
        except (OSError, ValueError) as exc:
            print("[错误] 读批量清单失败: %s" % exc)
            return 2
        rows = run_batch(sorted(cands.items()), today)
        _print_table(rows)
        return 3 if any(r["grade"] in ("C", "X") for _s, r in rows) else 0

    # --current: 当日全部源入口(与取数同口径 _pick)
    picked = scan_targets._pick(today.isoformat())
    seen = {}
    for _tier, _cn, _en, sid, url in picked:
        seen.setdefault(sid, url)
    rows = run_batch(sorted(seen.items()), today)
    _print_table(rows)
    return 3 if any(r["grade"] in ("C", "X") for _s, r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
