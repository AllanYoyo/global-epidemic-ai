#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""疫见全球 · 确定性取数器(2026-10-25 P4 引入)—— 脚本开车, LLM 只当顾问

为什么需要它(2026-10-25 定稿): Agent 驾驶的取数段对两种死法都是整轮全损 ——
轮次上限(50/50 打满, 候选囤在对话里 0 落盘)与 Token Plan 耗尽(429 切断)。
用户约束: 全链路 ≤10 分钟。本脚本把取数完全确定性化(零 LLM):

  当日源清单(scan_targets) → 探测复用(probe_channels, [dead] 跳过)
  → curl 入口页 → 抽链接 + 日期窗口过滤(7 天) → collect.py 存档(单源 ≤2 条,
  当日总条数默认不限 —— 2026-10-28 起, 取数零 LLM 后日上限不再是覆盖瓶颈)
  → candidates.jsonl 逐条追加 → search-log.md 逐源留痕

取舍(诚实登记): 链接启发式的召回率低于 Agent 浏览 —— 无日期线索的链接只记
search-log 当线索不存档; 需要搜索才能发现的条目(输华议定书类)本轮不覆盖。
覆盖率后续按源逐个补 pattern; 换来的是可预测、可测试、分钟级、零 token。

用法(由 run_scan.sh 调用):
  python3 scripts/fetch_pages.py --date 2026-10-25
  python3 scripts/fetch_pages.py --date 2026-10-25 --max-per-source 2 --max-total 0
退出码: 0=跑完(哪怕 0 条, 安静日合法); 2=参数/环境错误。
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from normalize import biz_today, DATA_DIR  # 业务时区与数据目录口径
import scan_targets
import probe_channels
import collect as collect_mod   # 存档计数口径与 collect.py 同源(数 *.meta.json)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FETCH_TIMEOUT = 15          # 单页 curl 超时(秒)
MAX_PER_SOURCE = 2          # 每源最多存档数(真正的防洪水位: 47 源理论天花板 ~94 条/日)
MAX_TOTAL = 0               # 当日存档总上限, 0 = 不限(2026-10-28 放开, 见 collect.py 同段注释)

# 链接启发式: 明显非文章的路径段(导航/页脚/订阅类)
SKIP_PATH = re.compile(
    r"/?(login|logout|register|search|subscribe|newsletter|rss|feed|contact|about|"
    r"help|faq|privacy|terms|cookie|sitemap|archive|tag|category|page/[0-9]+)",
    re.I)
# 「像文章」的路径特征(用于两段式验证: 列表页无日期线索时, 挑这种链接去文章页验日期)
ARTICLE_PATH = re.compile(
    r"/(noticias?|news|comunicad[oa]s?|prensa|press|resolucions?|resolutions?|"
    r"decretos?|decrees?|avisos?|notices?|boletin|bulletin|publicaciones|"
    r"publications|normas|legislacao|legislation|acts?|decisions?)/", re.I)
SLUG_LIKE = re.compile(r"/[a-z0-9][a-z0-9\-_]{12,}$", re.I)
# 日期形态: 2026-10-20 / 20/10/2026 / 20.10.2026 / 2026年10月20日 / Oct 20, 2026
#           / 24 de octubre de 2026(西) / 24 de outubro de 2026(葡)
DATE_PATTERNS = [
    re.compile(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})"),
    re.compile(r"(\d{1,2})[-/.](\d{1,2})[-/.](20\d{2})"),
    re.compile(r"(20\d{2})年(\d{1,2})月(\d{1,2})日"),
    re.compile(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{1,2}),?\s+(20\d{2})",
               re.I),
    re.compile(r"\d{1,2}\s+de\s+(enero|febrero|marzo|abril|mayo|junio|julio|agosto|"
               r"septiembre|setiembre|octubre|noviembre|diciembre)\s+de\s+(20\d{2})", re.I),
    re.compile(r"\d{1,2}\s+de\s+(janeiro|fevereiro|mar[cç]o|abril|maio|junho|julho|"
               r"agosto|setembro|outubro|novembro|dezembro)\s+de\s+(20\d{2})", re.I),
]
MONTHS_EN = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
MONTHS_ES = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
             "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
             "noviembre": 11, "diciembre": 12}
MONTHS_PT = {"janeiro": 1, "fevereiro": 2, "marco": 3, "março": 3, "abril": 4, "maio": 5,
             "junho": 6, "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10,
             "novembro": 11, "dezembro": 12}


def _clean(s):
    return re.sub(r"\s+", " ", (s or "").replace('"', "」").replace("`", "").replace("$", "")).strip()


def parse_date(text):
    """从一段文本里解析第一个日期, 返回 date 或 None(多语种)。"""
    if not text:
        return None
    for i, pat in enumerate(DATE_PATTERNS):
        m = pat.search(text)
        if not m:
            continue
        g = m.groups()
        try:
            if i == 0:
                y, mo, d = int(g[0]), int(g[1]), int(g[2])
            elif i == 1:
                d, mo, y = int(g[0]), int(g[1]), int(g[2])
            elif i == 2:
                y, mo, d = int(g[0]), int(g[1]), int(g[2])
            elif i == 3:
                mo = MONTHS_EN.get(g[0][:3].lower())
                if not mo:
                    return None
                d, y = int(g[1]), int(g[2])
            elif i == 4:
                mo = MONTHS_ES.get(g[0].lower())
                if not mo:
                    return None
                d, y = int(m.group(0).split()[0]), int(g[1])
            else:
                mo = MONTHS_PT.get(g[0].lower())
                if not mo:
                    return None
                d, y = int(m.group(0).split()[0]), int(g[1])
            return datetime.date(y, mo, d)
        except (ValueError, IndexError):
            continue
    return None


def in_window(d, today, days=7):
    return d is not None and (today - datetime.timedelta(days=days)) <= d <= today


def curl(url, timeout=FETCH_TIMEOUT, max_bytes=400000):
    """GET 一个 URL, 返回 (状态码, 文本)。委托 probe_channels.http_get
    (三层降级: --http1.1 / SSL 宽松重试 / 403 无 UA 重试 / 5xx 加长重试)。"""
    code, body, _note = probe_channels.http_get(url, timeout,
                                                want_body=True, max_bytes=max_bytes)
    return code, body


def extract_links(html, base_url):
    """抽 (href, 锚文本), 相对链接用 urljoin 解析成绝对(多数 CMS 新闻列表是相对路径,
    10-25 实测不解析相对链接会丢掉几乎全部新闻入口)。"""
    from urllib.parse import urljoin
    links = []
    for m in re.finditer(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
                         html, re.I | re.S):
        href, anchor = m.group(1).strip(), re.sub(r"<[^>]+>", " ", m.group(2))
        if href.startswith(("mailto:", "javascript:", "tel:", "#")):
            continue
        href = urljoin(base_url, href)
        if href.startswith("//"):
            href = "https:" + href
        if not href.startswith(("http://", "https://")):
            continue
        links.append((href, _clean(anchor)[:120]))
    return links


def _same_site(url, entry_url):
    """链接域 = 入口域 或其子域(粗粒度: 取末两段对比)。"""
    def host(u):
        return re.sub(r"^https?://", "", u).split("/")[0].lower().replace("www.", "")
    h1, h2 = host(url), host(entry_url)
    return h1 == h2 or h1.endswith("." + h2) or h2.endswith("." + h1)


def pick_candidates(links, entry_url, today):
    """两段式候选: 返回 (确认列表页日期在窗内的, 待文章页验证日期的)。

    列表页链接大多不带日期线索(10-25 实测 cl-sag 54 条链接 0 条带日期),
    日期在文章页里 —— 所以无日期但「像文章」的链接进入验证队列, 由
    verify_dates() 并行 curl 文章页解析日期(零 token); 旧闻兜底仍在
    normalize(--max-age-days)。
    """
    confirmed, to_verify, seen = [], [], set()
    for href, anchor in links:
        if href in seen or not _same_site(href, entry_url):
            continue
        path = href.split("?")[0]
        if SKIP_PATH.search(path):
            continue
        d = parse_date(anchor) or parse_date(path)
        if in_window(d, today):
            seen.add(href)
            confirmed.append((href, anchor or "(无标题)", d))
            continue
        # 无日期(或出窗但列表日期常不可靠): 像文章就去文章页验真
        looks_article = bool(ARTICLE_PATH.search(path) or SLUG_LIKE.search(path))
        if d is None and looks_article:
            seen.add(href)
            to_verify.append((href, anchor or "(无标题)"))
    confirmed.sort(key=lambda x: x[2] or today, reverse=True)
    # 新闻路径(noticias/news/...)优先于通用 slug —— gov.br 型门户的栏目结构页
    # 会以数百条 slug 型链接淹没新闻, 取前 N 个验证时必须让新闻排前面
    to_verify.sort(key=lambda t: 0 if ARTICLE_PATH.search(t[0].split("?")[0]) else 1)
    return confirmed, to_verify


def page_publish_date(html):
    """从文章页 HTML 解析发布日期: 结构化线索优先, 正文兜底。

    优先级: JSON-LD datePublished / meta article:published_time / <time datetime>
    → 正文首个日期。gov.br 型门户 head 里有几十 KB JS, 去标签后正文日期会被
    挤出切片, 所以结构化线索必须先于正文扫描(10-25 实测教训)。
    """
    if not html:
        return None
    for pat in (
            re.compile(r"datePublished[\"']?\s*[:=]\s*[\"'](\d{4}-\d{2}-\d{2})", re.I),
            re.compile(r"article:published_time[\"']?\s*content=[\"'](\d{4}-\d{2}-\d{2})", re.I),
            re.compile(r"<time[^>]+datetime=[\"'](\d{4}-\d{2}-\d{2})", re.I),
            re.compile(r"(\d{4}-\d{2}-\d{2})T\d{2}:\d{2}")):
        m = pat.search(html)
        if m:
            d = parse_date(m.group(1))
            if d:
                return d
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html,
                  flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return parse_date(text[:20000])


def verify_dates(to_verify, today, per_source_cap=2):
    """并行 curl 候选文章页, 从正文解析发布日期, 只留窗口内的。

    返回 [(url, title, date)]。单页 15s、6 并发, 12 页最坏 ~30s —— 不占
    token 预算, 也不挤爆 10 分钟约束。
    """
    if not to_verify:
        return []
    to_verify = to_verify[:per_source_cap]
    with ThreadPoolExecutor(max_workers=6) as ex:
        pages = list(ex.map(lambda t: curl(t[0]), to_verify))
    out = []
    for (href, anchor), (code, html) in zip(to_verify, pages):
        if code < 200 or code >= 400 or not html:
            continue
        d = page_publish_date(html)
        if in_window(d, today):
            out.append((href, anchor, d))
    return out


def fetch(date_str, max_per_source=MAX_PER_SOURCE, max_total=MAX_TOTAL,
          max_archives_per_day=0, log=print):
    """max_archives_per_day: 透传给 collect.py 的当日存档条数上限。

    0 = 不限。collect 默认 0 与 MAX_TOTAL=0 同口径; 真正的防洪水位是
    max_per_source(单源 ≤2)。受控测试需要收紧时由调用方显式给值。
    2026-10-27: 旧名 max_files_per_day 语义是"目录文件数", 与全链路的"存档条数"
    口径不一致(每条存档 = 正文 + .meta.json 两个文件), 实际只存得下 6 条 ——
    现在统一为**存档条数**。
    """
    today = datetime.date.fromisoformat(date_str)
    raw_dir = os.path.join(DATA_DIR, "raw", date_str)
    os.makedirs(raw_dir, exist_ok=True)

    picked = scan_targets._pick(date_str)
    # 当日已有存档数(与 collect.py 同口径: 数 *.meta.json, 交付文件不计入)
    already = collect_mod.count_archives(raw_dir)

    # 探测复用: [dead] 跳过(零工具调用, 与 Agent 版同口径); note 是降级重试留痕
    alive = {}
    for sid, code, state, _note in probe_channels.probe(date_str):
        alive[sid] = state == "ok"

    # 入口页**并行**抓取(21 源串行最坏 5 分钟会挤爆 10 分钟预算)
    todo = [(sid, url) for _t, _cn, en, sid, url in picked if alive.get(sid, False)]
    with ThreadPoolExecutor(max_workers=8) as ex:
        pages = dict(zip([t[0] for t in todo],
                         ex.map(lambda t: curl(t[1]), todo)))

    # 已存档的 url(candidates 合并去重用)
    done_urls = set()
    cand_path = os.path.join(raw_dir, "candidates.jsonl")

    def _append_candidate(rec):
        with open(cand_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    if os.path.isfile(cand_path):
        with open(cand_path, encoding="utf-8") as f:
            for line in f:
                try:
                    done_urls.add(json.loads(line).get("url"))
                except ValueError:
                    pass

    n_archived = 0
    log_lines = []
    for tier, cn, en, sid, url in picked:
        if not alive.get(sid, False):
            log_lines.append("| %s | 不可用 | 预探 dead |" % sid)
            continue
        if max_total and already + n_archived >= max_total:
            log_lines.append("| %s | 跳过 | 当日存档已达上限 %d |" % (sid, max_total))
            continue
        code, html = pages.get(sid, (0, ""))
        if code < 200 or code >= 400 or not html:
            log_lines.append("| %s | 不可用 | 入口页 HTTP %s |" % (sid, code or "失败"))
            continue
        confirmed, to_verify = pick_candidates(extract_links(html, url), url, today)
        # 无日期线索的文章型链接 → 并行验文章页日期(零 token)
        verified = verify_dates(to_verify, today)
        cands = confirmed + verified
        cands.sort(key=lambda x: x[2] or today, reverse=True)
        took = 0
        for link, title, d in cands:
            if took >= max_per_source or (max_total and already + n_archived >= max_total):
                break
            if link in done_urls:
                continue
            p = subprocess.run(
                [sys.executable, os.path.join(REPO, "scripts", "collect.py"),
                 "--url", link, "--title", title[:80], "--date", date_str,
                 "--max-archives-per-day", str(max_archives_per_day)],
                capture_output=True, text=True, timeout=60)
            if p.returncode != 0:
                out = (p.stdout or "") + (p.stderr or "")
                log_lines.append("| %s | 拒收 | %s |" % (sid, _clean(out)[:60]))
                if "[拒绝]" in out:
                    # collect.py 日上限触顶 —— 尊重上限, 停止全部后续存档
                    log_lines.append("| (全局) | 停止 | 当日存档触顶(collect 拒收) |")
                    return n_archived, log_lines, True
                continue
            n_archived += 1
            took += 1
            done_urls.add(link)
            _append_candidate({"title": title[:120], "url": link, "country_en": en,
                               "publish_date": d.isoformat(), "source_name": sid,
                               "brief": "", "topics": []})
            log_lines.append("| %s | 存档 | %s %s |" % (sid, d, link[:70]))
        if took == 0:
            log_lines.append("| %s | 无窗口内条目 | 链接 %d, 列表页带窗内日期 %d, 文章页验证通过 %d |"
                             % (sid, len(extract_links(html, url)), len(confirmed), len(verified)))
    return n_archived, log_lines, False


def main():
    ap = argparse.ArgumentParser(description="确定性取数(curl, 零 LLM)")
    ap.add_argument("--date", default=biz_today(), help="业务日期 YYYY-MM-DD(默认东八区今天)")
    ap.add_argument("--max-per-source", type=int, default=MAX_PER_SOURCE)
    ap.add_argument("--max-total", type=int, default=MAX_TOTAL)
    ap.add_argument("--max-archives-per-day", type=int, default=None,
                    help="透传 collect.py 的当日存档条数上限(默认 0=不限, 与日常同口径; "
                         "设正数可收紧, 测试/控量用)")
    ap.add_argument("--max-files-per-day", type=int, default=None,
                    help="[旧名, 已更名] 同 --max-archives-per-day")
    args = ap.parse_args()
    # 新旧名解析: 两个都给且不一致 → 报错退出(上限口径歧义会让配额变成猜)
    cap = args.max_archives_per_day
    if args.max_files_per_day is not None:
        if cap is not None and cap != args.max_files_per_day:
            print("[错误] --max-archives-per-day(%s) 与 --max-files-per-day(%s) 不一致, 已中止。"
                  % (cap, args.max_files_per_day))
            return 2
        cap = args.max_files_per_day
    if cap is None:
        cap = collect_mod.DEFAULT_MAX_ARCHIVES_PER_DAY  # 与 collect.py 默认同源(0=不限)
    try:
        n, log_lines, capped = fetch(args.date, args.max_per_source, args.max_total, cap)
    except (OSError, ValueError) as exc:
        print("[fetch_pages][错误] %s" % exc)
        return 2
    raw_dir = os.path.join(DATA_DIR, "raw", args.date)
    with open(os.path.join(raw_dir, "search-log.md"), "a", encoding="utf-8") as f:
        f.write("\n## 确定性取数(fetch_pages) %s\n\n" % datetime.datetime.now().strftime("%F %T"))
        for line in log_lines:
            f.write(line + "\n")
    print("[fetch_pages] %s: 新存档 %d 条(上限 %s)%s" % (
        args.date, n, args.max_total, ", 触顶停止" if capped else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
