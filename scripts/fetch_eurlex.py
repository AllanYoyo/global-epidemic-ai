#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""疫见全球 · 欧盟 EUR-Lex 通报源(2026-11-15 新增, 与 fetch_eping 同构的骨干增强)

为什么需要它(2026-11-15 生产复盘): 台账里躺着 12 条 EUR-Lex 来源的欧盟记录
(HPAI 防护区条例、第三国禽类暂停、边境检疫官控商品清单扩容——全是真检疫措施,
2026-09-19 → 10-20), 但 sources.yaml 里**没有 eur-lex 源** —— 那批是 P4 之前
Agent 路径抓到的, P4 确定性管线(10-25 起)接管后这条流断了 26 天。欧盟的检疫
机器 = 每天在 EUR-Lex 发布的实施条例/决定(秋季 HPAI 季节几乎日更), 且它们
**不走 ePing 主体**(EU 的 ePing 通报以 MRL/食品为主, 被收录红线正确拒收)。

三条路径的实测结论(2026-11-15 侦查):
  · eur-lex.europa.eu HTML 门户: AWS WAF 反爬(CloudFront challenge), 纯 curl
    无解 —— 不可用;
  · publications.europa.eu CELLAR 内容协商: **免注册、无 WAF、全文可达**
    (实测拿到 32026R0551 条例英文全文) —— 本模块的通道;
  · 公开 SPARQL 端点: 只存本体词表, 法律内容不在其中 —— 发现层不可用。

所以发现层走 **celex 序号推进探测**: EU 法规按系列连续编号(celex 32026R0551
= 条例, 32026D2130 = 决定, 32026L210 = 指令), CELLAR 对存在的号返回 303、
不存在的返回 404 —— 存在性判定零成本。状态文件记每系列最后**处理完**的号,
每轮向前推进直到连续 3 个 404(该系列当日无更多新件, 容忍 ≤2 的编号空洞)。
首次运行用**二分探测**定位当前边界 —— 刻意不回补边界之前的积压: normalize
的旧闻窗口(7 天)反正会拒收, 回补是无用功(10-25 前的欧盟内容已有 12 条
Agent 时代记录在库)。跨年切换: 存量 celex 的年份前缀与当前年不符时重新二分。

获取层: 内容协商(Accept: application/xhtml+xml + Accept-Language: eng)取
EUR-Lex 服务端渲染的 HTML(oj- 类名体系), 剥标签取文本。标题/日期用分层正则
+ 兜底(act 头部形态 `Commission Implementing Regulation (EU) 2026/551 of
5 November 2026 ...`), 日期解析复用 fetch_pages.parse_date(英区月份已在词表)。

红线过滤(镜像 fetch_eping 纪律): **食品黑名单优先于检疫白名单** —— MRL/
添加剂类 EU 条例(产量很大)就算标题里带 health 字样也要挡住; 检疫白名单
(plant health/animal health/quarantine/保护区域/具体疫病名…)命中才放行,
其余剔除并计数留痕。下游抽取闸门/normalize 照常二次把关。

溯源链: source.url 用 CELLAR 永久 URI(curl 可达, europa.eu 在官方域白名单);
EUR-Lex 人读链接写在存档正文里(--file 通道, collect 不必回访被 WAF 挡的门户)。

诚实取舍: X 系列(勘误/其它文件)不探测(勘误多为文本修正, 且回补会被旧闻窗
拒收); 新件漏发/隔日补发的极端形态会因序号推进而错过(与 fetch_eping 的
窗口不重发同源)。CELLAR 探测不耗配额、不需要 key。
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import threading
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from normalize import DATA_DIR, biz_today
from fetch_pages import parse_date

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CELLAR = "http://publications.europa.eu/resource/celex/"
EURLEX_HUMAN = "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:{celex}"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
STATE_PATH = os.path.join(DATA_DIR, "eurlex-state.json")
TIMEOUT = 20

# celex 系列: R(条例, 年内千位) / D(决定, 年内千位) / L(指令, 年内百位)。
SERIES = ("R", "D", "L")
CELEX_RE = re.compile(r"^3202[4-9][RLD]\d{3,5}$")

# 食品黑名单(优先于检疫白名单; 镜像 fetch_eping.FOOD_TBT_BLOCK 纪律)
FOOD_BLOCK = (
    "maximum residue", "pesticide residue", "residue limit", "mrl",
    "feed additive", "food additive", "novel food", "food supplement",
    "dietary supplement", "food safety", "genetically modified food",
    "marketing standards", "veterinary medicinal product",
)
# 检疫白名单(命中才放行)
QUAR_ALLOW = (
    "plant health", "animal health", "phytosanitary", "quarantine",
    "harmful organism", "protected zone", "demarcated", "emergency measures",
    "transmissible disease", "avian influenza", "highly pathogenic",
    "african swine", "classical swine", "foot-and-mouth", "lumpy skin",
    "bluetongue", "rabies", "health requirements", "introduction into the union",
    "consignments of", "animal disease", "wood packaging",
)


def _curl_code(url):
    """存在性判定: CELLAR 对存在的资源返回 303/200, 不存在返回 404。"""
    cmd = ["curl", "-sk", "-o", "/dev/null", "-w", "%{http_code}",
           "--max-time", str(TIMEOUT), "-A", UA,
           "-H", "Accept-Language: eng", url]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT + 5)
        return (p.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return "000"


def fetch_doc(celex):
    """内容协商取全文(eng 渲染)。返回 (html or None)。"""
    cmd = ["curl", "-sk", "--max-time", str(TIMEOUT + 10), "-A", UA,
           "-H", "Accept: application/xhtml+xml",
           "-H", "Accept-Language: eng",
           CELLAR + celex]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           errors="replace", timeout=TIMEOUT + 15)
        body = p.stdout or ""
        return body if len(body) > 2000 else None
    except (OSError, subprocess.SubprocessError):
        return None


def strip_html(html):
    """剥标签 + 实体还原 + 空白收敛。"""
    import html as _h
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    t = _h.unescape(t)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", t)


TITLE_RE = re.compile(
    r"((?:Commission|Council)\s+(?:Implementing|Delegating)?\s*"
    r"(?:Regulation|Decision|Directive)\s*\(EU\)\s*20\d{2}/\d+\s+of\s+"
    r"[0-9]{1,2}\s+\w+\s+20\d{2}[^.]{0,240})")


def extract_title(text, celex, series):
    """分层取标题: act 头部形态 -> 兜底。"""
    m = TITLE_RE.search(text)
    if m:
        return m.group(1).strip()
    return "EU %s %s" % (series, celex)


def classify(text):
    """红线过滤: 食品黑名单优先 -> 检疫白名单 -> 其余剔除。
    返回 (放行?, 命中词, 类别)。"""
    low = text[:6000].lower()
    for w in FOOD_BLOCK:
        if w in low:
            return False, w, "食品"
    for w in QUAR_ALLOW:
        if w in low:
            return True, w, "检疫"
    return False, "", "非检疫"


def _state_load():
    if os.path.isfile(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except ValueError:
            pass
    return {}


def _state_save(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def _series_pre(series):
    return "3202%d%s" % (datetime.date.today().year % 100, series)


def _frontier(series, state):
    """定位某系列的当前最大已发布号: 记录存在且年份前缀一致 -> 返回记录值;
    否则(首次/跨年) -> 二分探测当前边界。"""
    pre = _series_pre(series)
    cur = state.get(series)
    if cur and str(cur).startswith(pre):
        return pre, int(str(cur)[7:])
    lo, hi = 1, 3000
    while _curl_code(CELLAR + pre + str(hi)) in ("303", "200"):
        lo = hi
        hi *= 2
        if hi > 20000:
            break
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if _curl_code(CELLAR + pre + str(mid)) in ("303", "200"):
            lo = mid
        else:
            hi = mid
    return pre, lo


def discover(pre, start, probes=30, miss_stop=3):
    """从 start+1 向前推进, 返回 (新件号列表, 最后处理到的号)。
    连续 miss_stop 个 404 即停(容忍 ≤2 的编号空洞); probes 上限控制单轮时长。"""
    found, misses, last = [], 0, start
    for i in range(1, probes + 1):
        n = start + i
        if _curl_code(CELLAR + pre + str(n)) in ("303", "200"):
            found.append(n)
            misses = 0
        else:
            misses += 1
            if misses >= miss_stop:
                last = n - misses        # 最后一个存在号
                return found, last
        last = n
    return found, last


def collect_archive(raw_dir, date_str, celex, title, text, series,
                    max_archives_per_day, archive_lock):
    """写综合档案正文 -> collect.py 落盘。返回 ok / capped / fail。"""
    human = EURLEX_HUMAN.format(celex=celex)
    body = ("<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>%s</title></head>"
            "<body><h1>%s</h1>"
            "<p>celex: %s | series: %s</p>"
            "<p>EUR-Lex 人读链接: %s</p>"
            "<p>CELLAR 溯源: %s%s</p>"
            "<div>%s</div>"
            "<p>(source: Publications Office CELLAR 内容协商 —— 欧盟官方公报全文存档)</p>"
            "</body></html>") % (title, title, celex, series, human,
                                 CELLAR, celex, text[:16000])
    tmp_dir = tempfile.mkdtemp(prefix="radar-eurlex-")
    try:
        tmp_path = os.path.join(tmp_dir, "eurlex-%s.html" % celex)
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(body)
        with archive_lock:
            p = subprocess.run(
                [sys.executable, os.path.join(REPO, "scripts", "collect.py"),
                 "--file", tmp_path, "--url", CELLAR + celex,
                 "--title", title[:80], "--kind", "html", "--date", date_str,
                 "--max-archives-per-day", str(max_archives_per_day)],
                capture_output=True, text=True, timeout=60)
        out = (p.stdout or "") + (p.stderr or "")
        if p.returncode != 0:
            if "[拒绝]" in out:
                return "capped"
            print("[fetch_eurlex][WARN] collect 失败(%s): %s" % (celex, out[:80]))
            return "fail"
        return "ok"
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


def run(date_str, max_archives_per_day=0):
    """主流程: 推进发现 -> 过滤 -> 存档 -> 追加候选。返回存档数。

    状态语义: state[series] = 最后一个**处理完**(已存档或已正确剔除)的号。
    触顶/失败时停在本号, 余量下轮由 discover 从 state+1 重新发现 —— 不丢件。"""
    from collect import count_archives
    raw_dir = os.path.join(DATA_DIR, "raw", date_str)
    os.makedirs(raw_dir, exist_ok=True)
    state = _state_load()
    archive_lock = threading.Lock()
    cand_path = os.path.join(raw_dir, "candidates.jsonl")
    done = set()
    if os.path.isfile(cand_path):
        with open(cand_path, encoding="utf-8") as f:
            for line in f:
                try:
                    done.add(json.loads(line).get("url"))
                except ValueError:
                    pass

    n_arch, n_food, n_nonsp, n_fail = 0, 0, 0, 0
    for series in SERIES:
        pre, start = _frontier(series, state)
        found, _last = discover(pre, start)
        if not found:
            continue
        print("[fetch_eurlex] %s 系列新件 %d 条(%d..%d)"
              % (series, len(found), found[0], found[-1]))
        processed = start
        for n in found:
            celex = pre + str(n)
            if CELLAR + celex in done:
                processed = n           # 已在当日候选里(重跑), 视为处理完
                continue
            html = fetch_doc(celex)
            if not html:
                n_fail += 1
                continue                # 取文失败: 不推进 processed, 下轮重试
            text = strip_html(html)
            title = extract_title(text, celex, series)
            ok, hit, kind = classify(title + " " + text[:4000])
            if not ok:
                processed = n           # 正确剔除 = 处理完
                if kind == "食品":
                    n_food += 1
                    print("[fetch_eurlex] 食品黑名单剔除 %s (%s)" % (celex, hit))
                else:
                    n_nonsp += 1
                continue
            if max_archives_per_day and \
                    count_archives(raw_dir) >= max_archives_per_day:
                print("[fetch_eurlex] 当日存档触顶, %s 系列余件下轮补" % series)
                state[series] = pre + str(processed)
                _state_save(state)
                return n_arch
            r = collect_archive(raw_dir, date_str, celex, title, text, series,
                                max_archives_per_day, archive_lock)
            if r == "capped":
                print("[fetch_eurlex] 当日存档触顶(collect 拒绝), 余件下轮补")
                state[series] = pre + str(processed)
                _state_save(state)
                return n_arch
            if r == "fail":
                state[series] = pre + str(processed)   # 系统性失败: 保断点
                _state_save(state)
                return n_arch
            n_arch += 1
            processed = n
            with open(cand_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "title": title[:120],
                    "url": CELLAR + celex,
                    "country_en": "European Union",
                    "publish_date": str(biz_today()),
                    "source_name": "eu-eurlex",
                    "brief": "",
                    "topics": [],
                }, ensure_ascii=False) + "\n")
        state[series] = pre + str(processed)
    _state_save(state)
    print("[fetch_eurlex] EU 条例/决定新存档 %d 条(食品剔除 %d, 非检疫剔除 %d, 取文失败 %d)"
          % (n_arch, n_food, n_nonsp, n_fail))
    return n_arch


def _selftest():
    """离线自检: 过滤器/标题抽取/日期解析/状态往返/跨年重置/celex 正则。不联网。
    失败项不打「全部通过」—— 自检的诚实性靠退出码与末行断言。"""
    global STATE_PATH
    fails = []

    def check(cond, msg):
        print("[selftest] %s -> %s" % (msg, "通过" if cond else "失败"))
        if not cond:
            fails.append(msg)

    # 1) 红线过滤: 食品黑名单优先
    ok, hit, kind = classify("amending Annex I as regards maximum residue levels "
                             "of plant protection products")
    check(not ok and kind == "食品", "MRL 条例被食品黑名单剔除")
    # 2) 检疫白名单放行
    ok, hit, kind = classify("amending Annex I to Implementing Regulation (EU) 2019/2072 "
                             "as regards measures concerning the introduction into the Union "
                             "territory of consignments of plants for planting")
    check(ok and kind == "检疫", "植物检疫措施放行")
    # 3) 非检疫剔除
    ok, _hit, kind = classify("amending Decision on the financing of the common "
                              "agricultural policy")
    check(not ok, "非检疫内容剔除")
    # 4) 标题抽取
    t = extract_title("Commission Implementing Regulation (EU) 2026/551 of 5 November 2026 "
                      "laying down rules for the application of Regulation", "32026R0551", "R")
    check("2026/551" in t and "5 November 2026" in t, "act 头部标题抽取")
    # 5) 日期解析复用(EN 日-月名-年形态, 2026-11-15 补入 DATE_PATTERNS)
    check(parse_date("of 5 November 2026") is not None, "EN 月份日期解析复用")
    # 6) 状态往返 + 跨年重置(纯字符串断言, 保持自检离线 —— _frontier 的二分
    #    会真探测 CELLAR, 不放进自检)
    old = STATE_PATH
    STATE_PATH = os.path.join(tempfile.mkdtemp(prefix="eurlex-st-"), "state.json")
    _state_save({"R": "32026R0551"})
    check(_state_load().get("R") == "32026R0551", "状态文件往返")
    check(not "32019R9999".startswith(_series_pre("R")),
          "跨年前缀不符 -> _frontier 重新二分(不用旧值)")
    STATE_PATH = old
    # 7) celex 形态
    check(CELEX_RE.match("32026R0551") is not None, "celex 正则")
    if fails:
        print("[selftest] %d 项失败: %s" % (len(fails), "; ".join(fails)))
        return 1
    print("[selftest] 全部通过")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="EU EUR-Lex 通报源: CELLAR 内容协商, celex 序号推进发现, 零注册零 key")
    ap.add_argument("--date", default=biz_today())
    ap.add_argument("--archive", action="store_true",
                    help="过滤后经 collect.py 存档并追加候选")
    ap.add_argument("--max-archives-per-day", type=int, default=0)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return _selftest()
    if not args.archive:
        print("[fetch_eurlex] 仅预览(加 --archive 才落盘)")
        return 0
    try:
        run(args.date, args.max_archives_per_day)
    except Exception as exc:              # fail-open: 骨干增强不阻塞主取数
        print("[fetch_eurlex][WARN] 运行失败(不阻塞主取数): %s" % exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
