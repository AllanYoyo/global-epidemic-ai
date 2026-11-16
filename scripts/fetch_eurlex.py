#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""疫见全球 · 欧盟 EUR-Lex 通报源(2026-11-15 建源, 11-17 Web Services 版)

为什么需要它(生产复盘): 台账里躺着 12 条 EUR-Lex 来源的欧盟记录(HPAI 防护区
条例、第三国禽类暂停、边境检疫官控商品清单扩容——全是真检疫措施, 2026-09-19
→ 10-20), 但 sources.yaml 里**没有 eur-lex 源** —— 那批是 P4 之前 Agent 路径
抓到的, P4 确定性管线(10-25 起)接管后这条流断了 26 天。欧盟的检疫机器 = 每天
在 EUR-Lex 发布的实施条例/决定(秋季 HPAI 季节几乎日更), 且它们**不走 ePing
主体**(EU 的 ePing 通报以 MRL/食品为主, 被收录红线正确拒收)。

路径侦查与定线(全部实测, 详见 git log):
  · eur-lex.europa.eu HTML 门户: AWS WAF 反爬(CloudFront challenge), 纯 curl
    无解 —— 2026-11-17 用户注册 Web Services 并提交调用方 IP 后白名单放行
    (实测门户 202 -> 200);
  · publications.europa.eu CELLAR 内容协商: 免注册、无 WAF、全文可达(实测
    拿到 32026R0551 条例英文全文) —— 获取层通道;
  · 公开 SPARQL 端点: 只存本体词表, 法律内容不在其中;
  · 中间迭代的教训: EU 年度编号 2026/NNNN 是**跨机构共享序列**(R/L/D 交错,
    实测 32026R1500 不存在而 32026R2400 存在), 且未刊发决定不占号位 ->
    1473 之后连续 200+ 空号位 —— **按号码走路发现不可行**(按系列/共享号/
    并行大 miss_stop 三轮都穿不过去), 最终由 Web Services 的日期查询取代。

最终发现层: SOAP doQuery(端点 https://eur-lex.europa.eu/EURLexWebService,
SOAP 1.2 + WS-Security UsernameToken11, WSDL eurlex-ws?wsdl, 请求模式
eurlex-ws?xsd=3 —— searchRequest = expertQuery/page/pageSize/searchLanguage,
子元素必须带 elx: 前缀(elementFormDefault=qualified), searchLanguage 用
两位码 en)。expertQuery=PD>=日期 分页返回 NOTICE 元数据(celex/标题/OJ 发布
日期) —— **发现所需全部信息在一次查询里**。凭据: 注册批准邮件里的**专用
webservice 用户名**(生成的 ID, 不是 EU Login 邮箱 —— 实测邮箱认证
FailedAuthentication), 与密码一起放 ~/.hermes/.env(仓库永不入库)。

获取层: CELLAR 内容协商(Accept: application/xhtml+xml + Accept-Language:
eng, 必须 -L 跟随 303 重定向)取全文, 剥标签取文本。

红线过滤(镜像 fetch_eping 纪律): **食品黑名单优先于检疫白名单** —— MRL/
添加剂类 EU 条例(产量很大)就算标题里带 health 字样也要挡住; 检疫白名单
(plant health/animal health/quarantine/保护区域/具体疫病名…)命中才放行,
其余剔除并计数留痕。判据粒度是标题(EU act 标题几乎必含主题词); 正文级
主题由下游 LLM 抽取二次把关。下游抽取闸门/normalize 照常三重把关。

溯源链: source.url 用 CELLAR 永久 URI(curl 可达, europa.eu 在官方域白名单);
EUR-Lex 人读链接写在存档正文里(--file 通道, collect 不必回访被 WAF 挡的门户)。

诚实取舍: 标题级主题判据会漏掉"主题只在正文"的少数 act(下游归零风险与
fetch_eping 的动作预筛同源); 配额 1000 次/日, 本模块日耗 ~1-5 次SOAP 调用
+ N 次 CELLAR 取文, 远在配额内。
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
TIMEOUT = 30
SOAP_ENDPOINT = "https://eur-lex.europa.eu/EURLexWebService"
SOAP_ACTION = "https://eur-lex.europa.eu/EURLexWebService/doQuery"
PAGE_SIZE = 100
WINDOW_DAYS = 7          # 与 normalize 旧闻窗口对齐(PD>=今天-7)

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

ENV_TPL = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"
               xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
               xmlns:elx="http://eur-lex.europa.eu/search">
  <soap:Header>
    <wsse:Security soap:mustUnderstand="true">
      <wsse:UsernameToken>
        <wsse:Username>%s</wsse:Username>
        <wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordText">%s</wsse:Password>
      </wsse:UsernameToken>
    </wsse:Security>
  </soap:Header>
  <soap:Body>
    <elx:searchRequest>
      <elx:expertQuery>%s</elx:expertQuery>
      <elx:page>%s</elx:page>
      <elx:pageSize>%s</elx:pageSize>
      <elx:searchLanguage>en</elx:searchLanguage>
    </elx:searchRequest>
  </soap:Body>
</soap:Envelope>"""


def _load_ws_creds():
    """从 ~/.hermes/.env 读 EUR-Lex Web Services 凭据(与 EPING_API_KEY 同纪律,
    仓库永不入库)。用户名是注册批准邮件里的**专用 webservice 用户名**(生成的
    ID, 不是 EU Login 邮箱 —— 实测邮箱认证 FailedAuthentication)。"""
    env = {}
    path = os.path.join(os.path.expanduser("~"), ".hermes", ".env")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v
    user = env.get("EURLEX_WS_USER") or env.get("EURLEX_WS_EMAIL") or ""
    pwd = env.get("EURLEX_WS_PASSWORD") or ""
    return user, pwd


def soap_query(expert_query, page, creds):
    """SOAP 1.2 doQuery(SOAPAction + WS-Security UsernameToken11 明文密码)。
    请求体元素必须全部带 elx: 前缀(elementFormDefault=qualified, 实测空
    namespace 报 unexpected element)。返回响应文本(失败/空返回空串)。"""
    user, pwd = creds
    body = ENV_TPL % (user, pwd, expert_query, page, PAGE_SIZE)
    cmd = ["curl", "-sk", "--max-time", str(TIMEOUT + 20), "-A", UA,
           "-H", "Content-Type: application/soap+xml; charset=utf-8",
           "-H", "SOAPAction: \"%s\"" % SOAP_ACTION,
           "-X", "POST", "--data-binary", body, SOAP_ENDPOINT]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           errors="replace", timeout=TIMEOUT + 30)
        return p.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


def parse_notices(xml_text):
    """从 doQuery 响应解析 NOTICE 元数据, 返回 (totalhits, [{celex,title,pubdate}])。
    逐块正则(响应结构由我们自己的查询决定, 形状稳定; etree 的 SOAP 命名空间
    处理在此是噪声)。"""
    import html as _h
    th = re.search(r"<totalhits>(\d+)</totalhits>", xml_text)
    total = int(th.group(1)) if th else 0
    out = []
    for block in xml_text.split("<NOTICE>")[1:]:
        block = block.split("</NOTICE>")[0]
        m_c = re.search(r"<ID_CELEX>\s*<VALUE>([^<]+)</VALUE>", block)
        if not m_c:
            continue
        m_t = re.search(r"<EXPRESSION_TITLE>\s*<LANG>en</LANG>\s*<VALUE>([^<]+)</VALUE>", block)
        m_d = re.search(r"<OFFICIAL-JOURNAL-ACT_DATE_PUBLICATION>\s*<VALUE>([^<]+)</VALUE>", block)
        title = _h.unescape(m_t.group(1)).strip() if m_t else ""
        pub = parse_date(m_d.group(1)) if m_d else None
        out.append({"celex": m_c.group(1).strip(), "title": title,
                    "pubdate": pub.isoformat() if pub else None})
    return total, out


def discover(creds, start_date):
    """PD>=start 的分页查询, 返回 (totalhits, [{celex,title,pubdate}])。
    空响应/故障 fail-open 返回空列表(本轮放弃, 下轮重试)。"""
    q = "PD&gt;=%s" % start_date.replace("-", "")
    page, total, items = 1, 0, []
    while True:
        resp = soap_query(q, page, creds)
        if not resp or "searchResults" not in resp:
            return total, items
        total, notices = parse_notices(resp)
        items.extend(notices)
        if not notices or len(items) >= total:
            break
        page += 1
    return total, items


def fetch_doc(celex):
    """CELLAR 内容协商取全文(eng 渲染)。返回 (html or None)。
    必须带 -L: celex 资源是 303 重定向到 CELLAR 分发件(实测不跟随拿到空体)。"""
    cmd = ["curl", "-skL", "--max-time", str(TIMEOUT + 10), "-A", UA,
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


def classify(title):
    """红线过滤: 食品黑名单优先 -> 检疫白名单 -> 其余剔除。
    返回 (放行?, 命中词, 类别)。窗口内 act 的主题判据是标题(EU act 标题
    几乎必含主题词; 正文级主题在下游 LLM 抽取里二次把关)。"""
    low = title.lower()
    for w in FOOD_BLOCK:
        if w in low:
            return False, w, "食品"
    for w in QUAR_ALLOW:
        if w in low:
            return True, w, "检疫"
    return False, "", "非检疫"


def collect_archive(raw_dir, date_str, celex, title, text,
                    max_archives_per_day, archive_lock):
    """写综合档案正文 -> collect.py 落盘。返回 ok / capped / fail。"""
    human = EURLEX_HUMAN.format(celex=celex)
    body = ("<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>%s</title></head>"
            "<body><h1>%s</h1>"
            "<p>celex: %s</p>"
            "<p>EUR-Lex 人读链接: %s</p>"
            "<p>CELLAR 溯源: %s%s</p>"
            "<div>%s</div>"
            "<p>(source: Publications Office CELLAR 内容协商 —— 欧盟官方公报全文存档)</p>"
            "</body></html>") % (title, title, celex, human,
                                 CELLAR, celex, text[:16000])
    tmp_dir = tempfile.mkdtemp(prefix="radar-eurlex-")
    try:
        tmp_path = os.path.join(tmp_dir, "eurlex-%s.html"
                                % re.sub(r"[^A-Za-z0-9]", "_", celex))
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
    """主流程: SOAP 日期查询 -> 标题过滤 -> CELLAR 全文存档 -> 追加候选。
    返回存档数。无状态文件: 日期窗口由业务日期推导, done-URL 去重防重放。"""
    from collect import count_archives
    creds = _load_ws_creds()
    if not creds[0] or not creds[1]:
        print("[fetch_eurlex] 缺 EURLEX_WS_USER/EURLEX_WS_PASSWORD(~/.hermes/.env), 跳过本轮")
        return 0
    raw_dir = os.path.join(DATA_DIR, "raw", date_str)
    os.makedirs(raw_dir, exist_ok=True)
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

    today = datetime.date.fromisoformat(biz_today())
    start_date = (today - datetime.timedelta(days=WINDOW_DAYS)).isoformat()
    total, items = discover(creds, start_date)
    if not items:
        print("[fetch_eurlex] PD>=%s 无结果(或查询失败), 本轮 0 件" % start_date)
        return 0
    print("[fetch_eurlex] PD>=%s 共 %d 条, 过滤后处理" % (start_date, total))

    n_arch, n_food, n_nonsp, n_fail = 0, 0, 0, 0
    for it in items:
        url = CELLAR + it["celex"]
        if url in done:
            continue                    # 当日已处理(重跑去重)
        ok, hit, kind = classify(it["title"])
        if not ok:
            if kind == "食品":
                n_food += 1
                print("[fetch_eurlex] 食品黑名单剔除 %s (%s)" % (it["celex"], hit))
            else:
                n_nonsp += 1
            continue
        if max_archives_per_day and \
                count_archives(raw_dir) >= max_archives_per_day:
            print("[fetch_eurlex] 当日存档触顶, 余件下轮补(done-URL 去重不会丢件)")
            return n_arch
        html = fetch_doc(it["celex"])
        if not html:
            n_fail += 1
            print("[fetch_eurlex][WARN] 取文失败 %s(下轮 done-URL 未记, 自动重试)" % it["celex"])
            continue
        text = strip_html(html)
        title = it["title"] or ("EU %s" % it["celex"])
        r = collect_archive(raw_dir, date_str, it["celex"], title, text,
                            max_archives_per_day, archive_lock)
        if r == "capped":
            print("[fetch_eurlex] 当日存档触顶(collect 拒绝), 余件下轮补")
            return n_arch
        if r == "fail":
            return n_arch               # 系统性失败: 停本轮保断点
        n_arch += 1
        with open(cand_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "title": title[:120],
                "url": url,
                "country_en": "European Union",
                "publish_date": it["pubdate"] or str(biz_today()),
                "source_name": "eu-eurlex",
                "brief": "",
                "topics": [],
            }, ensure_ascii=False) + "\n")
    print("[fetch_eurlex] EU act 新存档 %d 条(食品剔除 %d, 非检疫剔除 %d, 取文失败 %d)"
          % (n_arch, n_food, n_nonsp, n_fail))
    return n_arch


def _selftest():
    """离线自检: 过滤器/NOTICE 解析/ENV 模板/窗口日期。不联网。"""
    fails = []

    def check(cond, msg):
        print("[selftest] %s -> %s" % (msg, "通过" if cond else "失败"))
        if not cond:
            fails.append(msg)

    # 1) 红线过滤: 食品黑名单优先
    ok, hit, kind = classify("Amending Annex I as regards maximum residue levels "
                             "of plant protection products")
    check(not ok and kind == "食品", "MRL 条例被食品黑名单剔除")
    # 2) 检疫白名单放行
    ok, hit, kind = classify("Measures concerning the introduction into the Union "
                             "territory of consignments of plants for planting")
    check(ok and kind == "检疫", "植物检疫措施放行")
    # 3) 非检疫剔除
    ok, _hit, kind = classify("Amending Decision on the financing of the common "
                              "agricultural policy")
    check(not ok, "非检疫内容剔除")
    # 4) 空标题不放行
    ok, _hit, kind = classify("")
    check(not ok, "空标题不放行")
    # 5) 合成 NOTICE 解析(含 en 标题/celex/OJ 日期)
    sample = ("<?xml version=\"1.0\"?><env:Envelope xmlns:env=\"http://www.w3.org/2003/05/soap-envelope\">"
              "<env:Body><ns1:searchResults xmlns:ns1=\"http://eur-lex.europa.eu/search\">"
              "<numhits>1</numhits><totalhits>1</totalhits>"
              "<result><reference>eng_cellar:abc_en</reference><rank>1</rank><content>"
              "<DTS_SUBDOM>LEGISLATION</DTS_SUBDOM><NOTICE><EXPRESSION>"
              "<EXPRESSION_TITLE><LANG>en</LANG><VALUE>Commission Implementing "
              "Regulation (EU) 2026/551 of 5 November 2026 laying down rules</VALUE>"
              "</EXPRESSION_TITLE></EXPRESSION><WORK><ID_CELEX><VALUE>32026R0551</VALUE>"
              "</ID_CELEX><OFFICIAL-JOURNAL-ACT_DATE_PUBLICATION><VALUE>2026-11-05</VALUE>"
              "</OFFICIAL-JOURNAL-ACT_DATE_PUBLICATION></WORK></NOTICE></content>"
              "</result></ns1:searchResults></env:Body></env:Envelope>")
    total, items = parse_notices(sample)
    check(total == 1 and len(items) == 1 and items[0]["celex"] == "32026R0551"
          and "2026/551" in items[0]["title"] and items[0]["pubdate"] == "2026-11-05",
          "合成 NOTICE 解析(celex/标题/日期)")
    # 6) ENV 模板格式化冒烟
    body = ENV_TPL % ("u", "p", "PD&gt;=20261108", 1, PAGE_SIZE)
    check("elx:searchRequest" in body and "PD&gt;=20261108" in body
          and "elx:pageSize>100" in body, "ENV 模板格式化")
    # 7) 窗口日期形态
    today = datetime.date.fromisoformat(biz_today())
    check(re.match(r"^\d{8}$", (today - datetime.timedelta(days=WINDOW_DAYS)).strftime("%Y%m%d"))
          is not None, "窗口日期 YYYYMMDD 形态")

    if fails:
        print("[selftest] %d 项失败: %s" % (len(fails), "; ".join(fails)))
        return 1
    print("[selftest] 全部通过")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="EU EUR-Lex 通报源: Web Services doQuery 发现 + CELLAR 全文存档")
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
