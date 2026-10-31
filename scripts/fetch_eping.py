#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""疫见全球 · ePing 通报骨干(2026-10-31 挂接)—— WTO 官方 API 确定性取数

为什么要有它(2026-10-31 用户拍板): 47 个国别源是「各国网站首页捞新闻」,
入口质量与站点可用性都不可控(17 死源 + 28 假活的实证)。WTO ePing 的
SPS/TBT 通报**本身就是政策法规变化的官方载体**(成员国政府主动通报的措施
草案/最终稿), 一个上游确定性覆盖全部 WTO 成员 —— 这是 measures 面的骨干。

数据源: WTO 官方 API `https://api.wto.org/eping`(开放规格 eping.yaml):
  · GET /notifications/search —— 按 distributionDateFrom/To + countryIds 检索,
    分页(pageSize max 100); 通报自带 title/description/productsFreeText/
    keywords/documentSymbol/notifiedDocumentLink(通报 PDF 直链);
  · GET /members —— 成员码表(countryIds 用 value 字段, 如 C840=美国);
  · 认证: 请求头 Ocp-Apim-Subscription-Key=$EPING_API_KEY(key 放 ~/.hermes/.env,
    **永不入库**, 与 WECHAT_WEBHOOK 同纪律)。

与管线的衔接(不另起口径):
  · 落盘 data/raw/<日期>/eping.json —— 形状就是 API 原始 {items:[...]},
    `parse_eping.py eping.json` 直接可消费(_from_json 本来就认 items 形状);
  · --archive: 经 parse_eping 的同款预筛(_dist/_pdf/_kw/_is_action, 单一来源)
    → 把**API 返回的通报元数据**(title/description/文号/日期/直链)综合成
    通知档案 HTML → collect.py 落盘(溯源链第一环, wto.org 在官方域白名单)
    → candidates.jsonl 追加(source_name=wto-eping, country_en 按成员反查);
  · 只存有 notifiedDocumentLink PDF 直链的通报(溯源硬要求: source.url 必须
    落到真实原文; 无直链的 Addenda 留在 eping.json 当线索)。

红线: **中国成员码被刻意排除** —— 中国自身措施不入台账(AGENTS.md 红线);
非 WTO 成员(塞尔维亚/阿塞拜疆/朝鲜)与聚合站标签自动跳过并留痕。

用法:
  python scripts/fetch_eping.py --date 2026-10-31                # 只拉取+落盘
  python scripts/fetch_eping.py --date 2026-10-31 --archive      # 拉取+预筛+存档
  python scripts/fetch_eping.py --date 2026-10-31 --days 3       # 收窄窗口
  python scripts/fetch_eping.py --selftest                        # 离线自检(不联网)
退出码: 0=跑完; 2=参数/环境错误(key 缺失/无效)。run_scan.sh 里 fail-open 调用,
ePing 故障不阻塞国别源主取数。
"""
import argparse
import datetime
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from normalize import DATA_DIR
import scan_targets
import collect as collect_mod
import parse_eping

API_BASE = "https://api.wto.org/eping"
PAGE_SIZE = 100
PAGE_CAP = 20                 # 单次运行最多 20 页(2000 条) —— 防御性上限
MEMBERS_TTL_DAYS = 7
API_TIMEOUT = 30

# watch 国别标签(_pick 的 en) → WTO 成员名匹配(对 /members 的 name 归一后 re.search)。
# 欧盟成员(波兰/罗马尼亚)与欧盟委员会统一映射 European Union —— WTO 里以欧盟为成员。
# 刻意不配: China WTO/TBT-SPS notification portals(中国自身措施不入台账, 红线)、
# Eurasian Economic Commission / Germany FLI TSIS / IPPC / WTO ePing(聚合站标签)、
# Serbia / Azerbaijan / North Korea(非 WTO 成员)—— 未匹配标签一律打印留痕, 不算错。
MEMBER_ALIASES = {
    "Argentina": r"^argentina$",
    "Australia": r"^australia$",
    "Brazil": r"^brazil$",
    "Canada": r"^canada$",
    "Chile": r"^chile$",
    "Colombia MinAgricultura": r"^colombia",
    "India": r"^india$",
    "Indonesia": r"^indonesia$",
    "Japan": r"^japan$",
    "Kazakhstan": r"^kazakhstan$",
    "Laos": r"^lao\b|^lao people",
    "Malaysia": r"^malaysia$",
    "Mexico": r"^mexico$",
    "Mongolia": r"^mongolia$",
    "Myanmar": r"^myanmar",
    "New Zealand": r"^new zealand$",
    "Norway": r"^norway$",
    "European Commission SANTE": r"^european union",
    "Poland": r"^european union",
    "Romania": r"^european union",
    "Philippines": r"^philippines$",
    "Russia": r"^russian federation",
    "Saudi Arabia": r"^saudi",
    "South Africa": r"^south africa$",
    "South Korea": r"^republic of korea|^korea, rep",
    "Thailand": r"^thailand$",
    "Turkey": r"^turkiye|^turkey$",
    "Ukraine": r"^ukraine$",
    "United Kingdom": r"^united kingdom$",
    "United States": r"^united states",
    "Uruguay": r"^uruguay$",
    "Vietnam": r"^viet nam|^vietnam$",
}


def _api_get(path, params, key):
    """GET 一个 API 端点, 返回解析后的 JSON。401/403 给出可行动的报错。"""
    url = "%s%s?%s" % (API_BASE, path, urllib.parse.urlencode(params))
    req = urllib.request.Request(url, headers={"Ocp-Apim-Subscription-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        if exc.code in (401, 403):
            raise SystemExit("[fetch_eping][错误] EPING_API_KEY 无效或未订阅 ePing API "
                             "(HTTP %s) —— 去 apiportal.wto.org 检查订阅。%s" % (exc.code, body))
        if exc.code == 429:
            print("[fetch_eping][WARN] 429 限流, 退避 30s 后重试一次", file=sys.stderr)
            time.sleep(30)
            with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        raise SystemExit("[fetch_eping][错误] API HTTP %s: %s" % (exc.code, body))


def _load_members(data_dir, key):
    """拉成员码表, 缓存 data/eping-members.json(TTL 7 天)。返回原始列表。"""
    cache = os.path.join(data_dir, "eping-members.json")
    if os.path.isfile(cache):
        age_days = (time.time() - os.path.getmtime(cache)) / 86400.0
        if age_days < MEMBERS_TTL_DAYS:
            with open(cache, encoding="utf-8") as f:
                return json.load(f)
    data = _api_get("/members", {"language": "1"}, key)
    members = data if isinstance(data, list) else data.get("items") or data.get("value") or []
    with open(cache, "w", encoding="utf-8") as f:
        json.dump(members, f, ensure_ascii=False)
    return members


def match_members(labels, members):
    """watch 国别标签 → WTO 成员码。返回 (映射 {label: code}, 反查 {code: label}, 未匹配标签集)。

    反查表: 一个成员被多个标签映射(如欧盟 ← SANTE/Poland/Romania)时取字典序
    最先的标签, 保证 candidates 的 country_en 确定性。
    """
    matched, unmatched = {}, set()
    for label in sorted(labels):
        pat = MEMBER_ALIASES.get(label)
        if not pat:
            unmatched.add(label)
            continue
        rx = re.compile(pat)
        hit = None
        for m in members:
            name = str(m.get("text") or m.get("name") or m.get("description") or m.get("member") or "")
            if rx.search(parse_eping._fold(name)):
                hit = str(m.get("value") or m.get("id") or m.get("code") or "")
                break
        if hit:
            matched[label] = hit
        else:
            unmatched.add(label)
    reverse = {}
    for label in sorted(matched):
        reverse.setdefault(matched[label], label)
    return matched, reverse, unmatched


def search_notifications(date_str, days, codes, key, member_param="comma"):
    """按窗口 + 成员检索通报, 翻页拉全, 按 id 去重。"""
    d = datetime.date.fromisoformat(date_str)
    params = {
        "language": "1",
        "distributionDateFrom": (d - datetime.timedelta(days=days - 1)).isoformat(),
        "distributionDateTo": d.isoformat(),
        "pageSize": str(PAGE_SIZE),
    }
    if member_param == "repeat":
        tail = "".join("&countryIds=%s" % urllib.parse.quote(c) for c in codes)
    else:
        params["countryIds"] = ",".join(codes)
        tail = ""
    items, seen = [], set()
    for page in range(1, PAGE_CAP + 1):
        data = _api_get("/notifications/search", dict(params, page=str(page)), key)
        batch = data.get("items") or []
        for rec in batch:
            rid = str(rec.get("id") or rec.get("documentSymbol") or "")
            if rid and rid not in seen:
                seen.add(rid)
                items.append(rec)
        total = int(data.get("totalCount") or len(items))
        if len(items) >= total or not batch:
            break
    return items


def synth_html(rec):
    """把 API 通报元数据综合成档案 HTML(title/description/文号/日期/直链)。

    description 是 WTO API 原文(HTML 片段) —— **逐字保留**(转义还原一次),
    溯源链上抽取段的 source.quote 必须出自这份原文。
    """
    sym = str(rec.get("documentSymbol") or "").strip()
    title = str(rec.get("title") or "").strip() or sym or "WTO notification"
    desc = html.unescape(str(rec.get("description") or ""))
    links = [l for l in str(rec.get("notifiedDocumentLink") or "").split(",") if l.strip()]
    kw = parse_eping._kw(rec)
    return ("<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>%s</title></head>"
            "<body><h1>%s</h1>"
            "<p>documentSymbol: %s</p>"
            "<p>notifyingMember: %s | area: %s | type: %s</p>"
            "<p>distributionDate: %s | commentDeadline: %s</p>"
            "<p>products: %s</p>"
            "<p>keywords: %s</p>"
            "<p>official PDF: %s</p>"
            "<div>%s</div>"
            "<p>(source: WTO ePing API api.wto.org/eping —— 通报元数据存档, "
            "原文以上述 official PDF 为准)</p></body></html>"
            % (_clean(title), _clean(title), _clean(sym), _clean(rec.get("notifyingMember") or ""),
               _clean(rec.get("area") or ""), _clean(rec.get("notificationType") or ""),
               _clean(parse_eping._dist(rec) or ""), _clean(str(rec.get("commentDeadlineDate") or "")[:10]),
               _clean(rec.get("productsFreeText") or ""), _clean(kw),
               " , ".join(_clean(l) for l in links), desc))


def _clean(s):
    return re.sub(r"\s+", " ", (s or "").replace('"', "」").replace("`", "").replace("$", "")).strip()


def archive_items(items, date_str, raw_dir, max_archives_per_day, archive_lock):
    """预筛 + 存档 + 追加候选。返回 (存档数, 预筛数, 触顶?)。"""
    json_path = os.path.join(raw_dir, "eping.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"items": items}, f, ensure_ascii=False)
    recs = parse_eping._from_json(json_path)
    chosen = [r for r in recs if r["url"] and parse_eping._is_action(r) and r["dist"]]
    # 当日已存过的 URL 去重(重跑不重复占配额; 抽取段另有 done_urls 去重)
    done = set()
    cand_path = os.path.join(raw_dir, "candidates.jsonl")
    if os.path.isfile(cand_path):
        with open(cand_path, encoding="utf-8") as f:
            for line in f:
                try:
                    done.add(json.loads(line).get("url"))
                except ValueError:
                    pass

    tmp_dir = tempfile.mkdtemp(prefix="radar-eping-")
    n_arch = 0
    capped = False
    try:
        for rec in chosen:
            if rec["url"] in done:
                continue
            if max_archives_per_day and \
                    collect_mod.count_archives(raw_dir) >= max_archives_per_day:
                capped = True
                break
            tmp_path = os.path.join(tmp_dir, "eping-%s-%s.html"
                                    % (hashlib_tag(rec["sym"]), uuid_hex()))
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(synth_html(rec["raw"]))
            with archive_lock:
                p = subprocess.run(
                    [sys.executable, os.path.join(REPO, "scripts", "collect.py"),
                     "--file", tmp_path, "--url", rec["url"], "--title", rec["title"][:80],
                     "--kind", "html", "--date", date_str,
                     "--max-archives-per-day", str(max_archives_per_day)],
                    capture_output=True, text=True, timeout=60)
            out = (p.stdout or "") + (p.stderr or "")
            if p.returncode != 0:
                if "[拒绝]" in out:
                    capped = True
                    break
                print("[fetch_eping][WARN] collect 失败(%s): %s" % (rec["sym"], _clean(out)[:80]))
                break     # collect 崩了多半系统性(磁盘/权限), 停止本轮存档
            n_arch += 1
            with archive_lock:
                with open(cand_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "title": (rec["title"] or rec["sym"] or "")[:120],
                        "url": rec["url"],
                        "country_en": rec.get("_country_en") or rec["member"],
                        "publish_date": rec["dist"],
                        "source_name": "wto-eping",
                        "brief": "",
                        "topics": [],
                    }, ensure_ascii=False) + "\n")
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return n_arch, len(chosen), capped


def hashlib_tag(s):
    import hashlib
    return hashlib.sha1((s or "").encode("utf-8")).hexdigest()[:10]


def uuid_hex():
    import uuid
    return uuid.uuid4().hex[:8]


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _selftest():
    """离线自检: 成员别名匹配 / API 形状解析 / 直链提取 / 综合档案, 不联网。"""
    # 形状对齐 API 实测(2026-10-31 真跑): 成员表只有 value/text 两个字段
    members = [
        {"value": "C840", "text": "United States of America"},
        {"value": "C704", "text": "Viet Nam"},
        {"value": "C032", "text": "Argentina"},
        {"value": "C410", "text": "Republic of Korea"},
        {"value": "C764", "text": "Switzerland"},
        {"value": "U918", "text": "European Union"},
    ]
    labels = ["Vietnam", "United States", "South Korea", "European Commission SANTE",
              "China WTO/TBT-SPS notification portals", "Serbia", "WTO ePing"]
    matched, reverse, unmatched = match_members(labels, members)
    ok = True
    def check(cond, msg):
        nonlocal ok
        print("[selftest] %s → %s" % (msg, "通过" if cond else "失败"))
        ok = ok and cond
    check(matched.get("Vietnam") == "C704", "Vietnam → Viet Nam(C704)")
    check(matched.get("United States") == "C840", "United States → C840")
    check(matched.get("South Korea") == "C410", "South Korea → Republic of Korea")
    check(matched.get("European Commission SANTE") == "U918", "欧盟标签 → European Union(U918)")
    check("China WTO/TBT-SPS notification portals" in unmatched
          and "Serbia" in unmatched and "WTO ePing" in unmatched,
          "中国(红线)/非成员/聚合标签 正确跳过并留痕")
    check(reverse.get("C704") == "Vietnam", "反查表 code → 国别标签")
    sample = {
        "id": "19877", "area": "TBT", "notificationType": "Regular notification",
        "distributionDate": "2016-11-29T00:00:00.0000000+00:00",
        "documentSymbol": "G/TBT/N/USA/1234", "notifyingMember": "United States of America",
        "notifyingMemberCode": "C840",
        "title": "Import ban on beef and poultry products",
        "description": "&lt;p&gt;Establishes provisions governing imports.&lt;/p&gt;",
        "productsFreeText": "Beef; Poultry",
        "keywords": [{"id": 29, "name": "Food safety"}],
        "notifiedDocumentLink": "https://members.wto.org/crnattachments/2016/TBT/USA/16_4900_00_e.pdf",
    }
    tmp = tempfile.mkdtemp(prefix="radar-eping-selftest-")
    try:
        path = os.path.join(tmp, "eping.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"items": [sample]}, f)
        recs = parse_eping._from_json(path)
        r = recs[0]
        check(r["url"].startswith("https://members.wto.org/crnattachments"),
              "_pdf 认 API 的 notifiedDocumentLink")
        check(r["dist"] == "2016-11-29", "_dist 归一 API 的 distributionDate")
        check(r["kw"] == "Food safety", "_kw 对象数组取 name")
        check(parse_eping._is_action(r), "Import ban 标题命中动作词")
        page = synth_html(sample)
        check("G/TBT/N/USA/1234" in page and "crnattachments" in page
              and "Establishes provisions" in page, "综合档案含文号/直链/原文描述")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="ePing 通报骨干(WTO 官方 API 确定性取数)")
    ap.add_argument("--date", default=None, help="业务日期 YYYY-MM-DD(默认东八区今天)")
    ap.add_argument("--days", type=int, default=7, help="回看窗口天数(默认 7, 与旧闻窗同口径)")
    ap.add_argument("--archive", action="store_true", help="预筛后经 collect.py 存档并追加候选")
    ap.add_argument("--max-archives-per-day", type=int, default=0,
                    help="当日存档条数上限(默认 0=不限, 通报本身即政策形状)")
    ap.add_argument("--member-param", choices=("comma", "repeat"), default="comma",
                    help="countryIds 传参形状(规格未写明数组形状, 默认逗号连接, 400 时试 repeat)")
    ap.add_argument("--selftest", action="store_true", help="离线自检(不联网)")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    date_str = args.date or (datetime.datetime.utcnow()
                             + datetime.timedelta(hours=8)).date().isoformat()
    key = os.environ.get("EPING_API_KEY")
    if not key:
        print("[fetch_eping][错误] 缺 EPING_API_KEY(~/.hermes/.env 配置) —— 不拉取")
        return 2
    raw_dir = os.path.join(DATA_DIR, "raw", date_str)
    os.makedirs(raw_dir, exist_ok=True)

    picked = scan_targets._pick(date_str)
    labels = set()
    for _t, _cn, en, _sid, _u in picked:
        labels.add(en)
    members = _load_members(DATA_DIR, key)
    matched, reverse, unmatched = match_members(labels, members)
    if unmatched:
        print("[fetch_eping] 未匹配/跳过的标签(%d): %s"
              % (len(unmatched), ", ".join(sorted(unmatched))))
    if not matched:
        print("[fetch_eping][错误] 没有任何 watch 国别匹配到 WTO 成员 —— 成员码表变了? "
              "看 %s" % os.path.join(DATA_DIR, "eping-members.json"))
        return 2
    print("[fetch_eping] watch 成员 %d 个: %s"
          % (len(matched), ", ".join("%s=%s" % kv for kv in sorted(matched.items()))))

    items = search_notifications(date_str, args.days, sorted(set(matched.values())),
                                 key, args.member_param)
    print("[fetch_eping] 窗口 %s±%dd 拉到通报 %d 条"
          % (date_str, args.days, len(items)))
    if not args.archive:
        with open(os.path.join(raw_dir, "eping.json"), "w", encoding="utf-8") as f:
            json.dump({"items": items}, f, ensure_ascii=False)
        return 0

    # 逐条挂 country_en(成员码反查) —— candidates 的国别归属
    for it in items:
        code = str(it.get("notifyingMemberCode") or "")
        it["_country_en"] = reverse.get(code) or str(it.get("notifyingMember") or "-")
    n_arch, n_chosen, capped = archive_items(
        items, date_str, raw_dir, args.max_archives_per_day, threading.Lock())
    print("[fetch_eping] 动作预筛 %d 条 → 存档 %d 条%s"
          % (n_chosen, n_arch, "(触顶停止)" if capped else ""))
    with open(os.path.join(raw_dir, "search-log.md"), "a", encoding="utf-8") as f:
        f.write("\n## ePing 通报(WTO API) %s\n\n| wto-eping | ePing 通报 | 窗口 %d 天, 拉取 %d, "
                "预筛 %d, 存档 %d |\n"
                % (datetime.datetime.now().strftime("%F %T"), args.days, len(items),
                   n_chosen, n_arch))
    return 0


if __name__ == "__main__":
    sys.exit(main())
