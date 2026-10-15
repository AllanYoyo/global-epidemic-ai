#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""政策变化记录标准化入库。

本分支生产数据合同只有 record_type=policy。字段权威定义见
 docs/event-schema.md; 本脚本负责校验字段、生成稳定 event_id、写 JSON 与 SQLite。
event_id 已存在时默认合并更新: 空值与兜底默认值不覆盖已有核验/研判结论,
需要整行替换时显式传 --replace。
新记录带旧闻拦截: 发布时间(source.publish_date, 缺省 event_date)早于
 --max-age-days 窗口(默认 7 天, POLICY_MAX_AGE_DAYS 覆盖, 0 关闭)判为旧闻拒收;
 已入库 event_id 的合并更新不受影响, 防止常年挂网的旧公告被当"今日新增"入日报。
日期口径: 一律用本模块的 biz_today()/biz_now()(东八区), 不用宿主机 date.today()
 —— 生产 VPS 系统时区是 Etc/UTC, 直接取 UTC 日期会让北京时间早间跑批整体错一天。
"""
import argparse
import datetime
import hashlib
import json
import os
import sqlite3
from datetime import date as _date

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("EPIDEMIC_DATA_DIR") or os.path.join(REPO, "data")
DB_PATH = os.environ.get("EPIDEMIC_DB") or os.path.join(REPO, "database", "epidemic.db")
VERIF_STATUS = {"verified", "single_source", "unverified", "false_positive", "merged"}
POLICY_ACTIONS = {"收紧", "放松", "调整", "恢复"}
POLICY_DOMAINS = {"animal", "plant", "both", "trade", "measures"}
POLICY_IMPACT_TYPES = {"约束", "机会", "中性"}
POLICY_IMPACT_LEVELS = {"高影响", "中影响", "低影响"}
POLICY_CHINA_RELEVANCE = {"直接涉及中国", "间接影响", "暂无明显关联"}
# 合并更新时的兜底默认值: 不覆盖库中更明确的取值, 防止重抽取抹掉核验/研判结论
DEFAULT_SENTINELS = {"verification_status": "unverified", "policy_status": "已生效"}
# 旧闻拦截默认窗口(天): 新记录发布时间早于"今天-N 天"拒收; 0 或 --max-age-days 0 关闭
DEFAULT_MAX_AGE_DAYS = 7

# ---- 业务时区(东八区) ----
# 生产 VPS 系统时区是 Etc/UTC, 而 cron 按 Asia/Shanghai 触发。若各脚本直接用
# date.today(), 北京时间早上 07:30 跑批时 UTC 仍是前一天, 日报文件名 / 旧闻窗口
# / first_seen 会整体错一天(2026-10 实测: 10-15 扫描产出 2026-10-14-* 报告)。
# 统一走这里取业务日期, 不依赖宿主机 TZ。
TZ_OFFSET_HOURS = 8


def biz_today():
    """业务日期(东八区) YYYY-MM-DD。日报命名、旧闻窗口、归档目录都以此为准。"""
    return (datetime.datetime.utcnow()
            + datetime.timedelta(hours=TZ_OFFSET_HOURS)).date().isoformat()


def biz_now():
    """业务时间(东八区) ISO8601, 用于 first_seen / updated_at 等时间戳。"""
    return (datetime.datetime.utcnow()
            + datetime.timedelta(hours=TZ_OFFSET_HOURS)).isoformat(timespec="seconds")

# ---- 官方来源域名后缀(可溯源硬要求的落地口径) ----
# source.tier=1 只应由下列官方域名的原文支撑; 第三方站点(媒体/行业站/他国贸易门户/
# 商业数据库)即使被检索命中也只能作线索, 不得标 tier 1 或据此判 verified。
# 2026-10 实测 19 条入库记录 100% unverified, 其中逾半数来源是
# freyrsolutions.com / qpests.insect-genome.com / thebrief.com.na / www.evs.ee /
# bangladeshtradeportal.gov.bd / agrinfo.eu 这类非官方站点, 却被标成 tier 1。
OFFICIAL_DOMAIN_SUFFIXES = (
    # 国际组织与通报平台
    "wto.org", "woah.org", "fao.org", "ippc.int", "eppo.int",
    # 欧盟(EUR-Lex 为欧盟官方公报库; SANTE/DG SANTE 为措施发布主体)
    "europa.eu",
    # 北美
    "aphis.usda.gov", "usda.gov", "govinfo.gov", "canada.ca",
    # 英澳新(英国已脱欧, 有自有体系; 澳新为主要对华出口方)
    "gov.uk", "agriculture.gov.au",
    # 新西兰通用 .govt.nz 后缀(覆盖 mpi/beehive/afs 之外的兄弟站, 如 biosecurity.govt.nz)
    "govt.nz", "mpi.govt.nz", "beehive.govt.nz", "afs.govt.nz",
    # 亚洲
    "maff.go.jp", "mafra.go.kr", "mard.gov.vn", "dld.go.th", "doa.go.th",
    "dvs.gov.my", "pertanian.go.id", "gov.la", "gov.kz", "gov.mn",
    "bpi.da.gov.ph", "bai.gov.ph", "dahd.nic.in", "ppqs.gov.in",
    # 南美(注: 阿根廷/墨西哥官方门户主域是 gob.ar / gob.mx, 不是 gov.ar / gov.mx)
    "senasa.gov.ar", "sag.gob.cl", "gov.br", "mgap.gub.uy",
    "gob.ar", "gob.mx",
    # 欧亚 / 邻国 / 新增对华出口大户(2026-10 扩 sources.yaml 后补齐)
    "gov.ru", "gov.pl", "gov.ua", "gov.rs", "gov.mm", "gov.az",
    "gov.co", "gov.za", "gov.kp",
    # 特例 host(非标准政府域后缀, 但确实是该国官方门户)
    "tarimorman.gov.tr", "mewa.gov.sa", "nzda.govt.nz", "food.ec.europa.eu",
    "fli.de", "mattilsynet.no", "ansvsa.ro",
    "rshn-khv-eao.ru",  # 俄罗斯远东区域署, 主域是 .ru 但非 .gov.ru
    # 国际组织/其他官方登记系统
    "faolex.fao.org", "ecolex.org", "sanitary.gov",
    # ---- 次国家/区域官方门户(2026-10 依生产库 162 条实跑数据补齐) ----
    # 这批域在现网记录里真实命中且确属官方, 但不在 sources.yaml 的 track=policy
    # 源清单内(州级政府/单列口岸/特区政府), 检索层通用检索式会直接命中, 故必须放行。
    # 注意: 以下每条都对应生产库里真实出现过的 host, 不是预防性铺开。
    "gov",            # 美国受限顶级域: 一并覆盖联邦(aphis.usda.gov / federalregister.gov /
                      # whitehouse.gov / govinfo.gov)与州级(news.maryland.gov /
                      # www.tahc.texas.gov 等), 无需逐州罗列; 不会误伤 gov.bd / gob.mx 等
    "gov.hk",         # 中国香港特区政府新闻公报(info.gov.hk)
    "cfs.gov.hk",     # 香港食安中心
    "gov.sg",         # 新加坡政府(含食品局 sfa.gov.sg)
    "da.gov.ph",      # 菲律宾农业部
    "elperuano.pe",   # 秘鲁官方公报 El Peruano(Editora Perú 国企), 含检索入口 busquedas.*
    "unbs.go.ug",     # 乌干达国家标准局(东非 DEAS 通报参与方)
    "rada.gov.ua",    # 乌克兰议会官方法令库(закон.rada.gov.ua)
)

# 明显非官方的第三方站点(黑名单, 命中即警告; 命中黑名单的 tier 1 强制降为 3)
# 命中本身或其任意子域即判第三方。注意: 孟加拉只精确列 tradeportal 子域,
# 未来引入孟加拉政府官方源(dae.gov.bd / lsd.gov.bd)时不应被这条误伤。
THIRD_PARTY_HINTS = (
    "bangladeshtradeportal.gov.bd",  # 孟加拉贸易门户(转述他国措施)
    "insect-genome.com",             # 第三方检疫数据库(qpests.*)
    "thebrief.com", "thebrief.com.na",  # 纳米比亚商业通讯
    "agrinfo.eu",                    # 私营农业信息站
    "freyrsolutions.com",
    "evs.ee",
    # 2026-10 依生产库实跑数据补充: 均为"长得像官方但非官方"的域。
    # eurlexa.com 尤其危险——它是 EUR-Lex 的商业镜像, 自称 "EU legislation at
    # your fingertips", 真身是 eur-lex.europa.eu。生产库里曾用它支撑一条
    # "verified + tier 1" 的欧盟条例记录, 属可溯源硬要求的直接违规。
    "eurlexa.com",
    "economy.com.na",    # 私营 Namibia Economist 报纸 / IPPR 经济指南
    "base.spinform.ru",  # СПИНФОРМ 私营法律信息数据库(非俄政府门户)
    "quora.com", "wikipedia.org", "medium.com", "blogspot.com",
    "wordpress.com", "linkedin.com", "facebook.com", "twitter.com",
    "youtube.com",
)


def _source_host(url):
    """从 URL 取主机名(小写); 无法解析时返回空串。"""
    if not isinstance(url, str) or "//" not in url:
        return ""
    try:
        host = url.split("//", 1)[1].split("/", 1)[0]
    except IndexError:
        return ""
    return host.split("@")[-1].split(":")[0].strip().lower()


def _is_official_domain(host):
    """主机是否落在官方域名后缀内(含子域)。"""
    if not host:
        return False
    return any(host == s or host.endswith("." + s) for s in OFFICIAL_DOMAIN_SUFFIXES)


def _is_third_party(host):
    """主机是否命中第三方站点黑名单(按后缀匹配, 覆盖其子域)。"""
    if not host:
        return False
    return any(host == h or host.endswith("." + h) for h in THIRD_PARTY_HINTS)


def _host_matches(host, suffixes):
    return any(host == s or host.endswith("." + s) for s in suffixes)


def check_source_domain(e):
    """校验 source 与 tier 是否自洽。

    返回 (errors, normalized_event):
    - tier=1 但主机非官方域 -> 报错拒收(除非该国确无已知官方域, 用 source.official=1 显式声明)
    - 命中第三方黑名单 -> tier 强制降为 3, 且不得判 verified
    """
    errs = []
    src = e.get("source") or {}
    url = src.get("url")
    host = _source_host(url)
    tier = src.get("tier")

    if host and _is_third_party(host):
        # 第三方站点: 只能是线索。降级并禁止 verified。
        if tier == 1:
            src["tier"] = 3
        if e.get("verification_status") in ("verified",):
            errs.append("来源为第三方站点(%s), 不得判 verified(仅可线索)" % host)
        errs.append("[线索] 来源主机 %s 非官方站点, 已将 source.tier 降为 3; "
                    "须回溯该国官方原文后重新入库才能支撑 verified" % host)
        e["source"] = src
        return errs, e

    if tier == 1 and host and not _is_official_domain(host):
        if not src.get("official"):
            errs.append(
                "source.tier=1 但来源主机 %s 不在官方域名库内; "
                "若该国确有本系统未收录的官方域, 在 source 里显式加 official: 1 说明" % host)
        else:
            e.setdefault("verification_notes", None)
    return errs, e


def now():
    return biz_now()


def _subject_components(e):
    """计算 event_id 的 subject 关联对象: policy_key 或四个关联字段之一非空。"""
    products = e.get("products") or []
    targets = e.get("target_countries") or []
    return [
        (e.get("policy_key") or "").strip(),
        (e.get("disease_name_en") or "").strip().lower(),
        ",".join(sorted(str(x).strip().lower() for x in products if str(x).strip())),
        ",".join(sorted(str(x).strip().lower() for x in targets if str(x).strip())),
        (e.get("scope") or "").strip().lower(),
    ]


def event_id_of(e):
    products = e.get("products") or []
    targets = e.get("target_countries") or []
    subject = e.get("policy_key") or "|".join([
        str(e.get("disease_name_en") or "").strip().lower(),
        ",".join(sorted(str(x).strip().lower() for x in products)),
        ",".join(sorted(str(x).strip().lower() for x in targets)),
        str(e.get("scope") or "").strip().lower(),
    ])
    key = "|".join([
        "policy", str(e.get("country_en", "")).strip().lower(),
        str(e.get("policy_domain") or "").strip().lower(),
        str(e.get("action_type") or "").strip().lower(), subject,
        str(e.get("effective_date") or e.get("event_date") or "").strip(),
    ])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _norm_date(value):
    """把 YYYY / YYYY-MM 粒度补齐为 YYYY-MM-DD; 长度不合法返回 None(不猜)。"""
    d = str(value or "").strip()
    if len(d) == 4:
        return d + "-01-01"
    if len(d) == 7:
        return d + "-01"
    return d if len(d) == 10 else None


def published_date(e):
    """发布时间口径: 优先 source.publish_date(页面发布日), 缺省退回 event_date/effective_date。

    页面发布日是"这条情报何时出现"的真实信号; event_date 是公告/生效日期,
    今日新发布的公告完全可能引用更早的生效日, 不能用 event_date 反推旧闻。
    """
    src = e.get("source") if isinstance(e.get("source"), dict) else {}
    for cand in (src.get("publish_date"), e.get("event_date"), e.get("effective_date")):
        d = _norm_date(cand)
        if d:
            return d
    return None


def stale_reason(e, cutoff):
    """时间窗校验: 发布时间早于 cutoff(YYYY-MM-DD)判为旧闻, 返回拒收原因; 其余返回 None。"""
    if not cutoff:
        return None
    d = published_date(e)
    if d and d < cutoff:
        return "发布时间 %s 早于窗口下限 %s" % (d, cutoff)
    return None


def _event_exists(con, event_id):
    return con.execute(
        "SELECT 1 FROM events WHERE event_id = ?", (event_id,)).fetchone() is not None


def validate(e):
    errs = []
    if e.get("record_type") not in (None, "policy"):
        errs.append("record_type 必须为 policy")
    for key in ("country_cn", "country_en", "source"):
        if not e.get(key):
            errs.append("缺少必填字段 %s" % key)
    if not (e.get("event_date") or e.get("effective_date")):
        errs.append("政策记录需要 event_date 或 effective_date")
    if e.get("category") not in (None, "policy"):
        errs.append("政策记录的 category 必须为 policy")
    if e.get("action_type") and e["action_type"] not in POLICY_ACTIONS:
        errs.append("action_type 必须为 收紧|放松|调整|恢复")
    if e.get("policy_domain") and e["policy_domain"] not in POLICY_DOMAINS:
        errs.append("policy_domain 取值不合法")
    if not (e.get("title_cn") or e.get("title_en") or e.get("summary_cn")):
        errs.append("政策记录需要 title_cn / title_en / summary_cn 至少其一")
    # 关联对象必填: event_id 强依赖 subject, 否则同国/同领域/同动作/同日的两条政策
    # 会算出同一 event_id 并被默认合并吞掉。policy_key 是显式兜底。
    if not any(_subject_components(e)):
        errs.append(
            "event_id 关联对象必填: 至少给出 policy_key / disease_name_en / "
            "products / target_countries / scope 之一(原文未提及时给显式 policy_key 兜底)"
        )
    src = e.get("source")
    if not isinstance(src, dict) or not src.get("url"):
        errs.append("source.url 必填(可溯源是硬要求)")
    if isinstance(src, dict):
        if not src.get("quote"):
            errs.append("source.quote 必填")
        elif len(str(src["quote"])) > 120:
            errs.append("source.quote 不得超过 120 字")
    if e.get("verification_status") and e["verification_status"] not in VERIF_STATUS:
        errs.append("verification_status 非法: %s" % e["verification_status"])
    # 来源域名与 tier 自洽性: 第三方站点不得标 tier 1 / 不得判 verified
    dom_errs, _ = check_source_domain(e)
    errs.extend(dom_errs)
    if e.get("impact_type") and e["impact_type"] not in POLICY_IMPACT_TYPES:
        errs.append("impact_type 必须为 约束|机会|中性")
    if e.get("impact_level") and e["impact_level"] not in POLICY_IMPACT_LEVELS:
        errs.append("impact_level 必须为 高影响|中影响|低影响")
    if e.get("china_relevance") and e["china_relevance"] not in POLICY_CHINA_RELEVANCE:
        errs.append("china_relevance 取值不合法")
    return errs


def fill_defaults(e):
    e["record_type"] = "policy"
    e["category"] = "policy"
    e["event_date"] = e.get("effective_date") or e.get("event_date")
    d = str(e["event_date"]).strip()
    precision = e.get("date_precision")
    if len(d) == 4:
        d, precision = d + "-01-01", precision or "year"
    elif len(d) == 7:
        d, precision = d + "-01", precision or "month"
    else:
        precision = precision or "day"
    e["event_date"], e["date_precision"] = d, precision
    e.setdefault("policy_domain", "measures")
    e.setdefault("action_type", "调整")
    e.setdefault("policy_status", "已生效")
    e.setdefault("effective_date", e["event_date"])
    e.setdefault("impact_type", None)
    e.setdefault("impact_level", None)
    e.setdefault("impact_score", None)
    e.setdefault("impact_focus", None)
    e.setdefault("impact_rationale", None)
    e.setdefault("china_relevance", None)
    e.setdefault("recommended_action", None)
    e.setdefault("dimension_scores", {})
    e.setdefault("disease_name_cn", None)
    e.setdefault("disease_name_en", None)
    e.setdefault("region", None)
    e.setdefault("cross_sources", [])
    e.setdefault("checked_urls", [])
    e.setdefault("verification_status", "unverified")
    e.setdefault("summary_cn", None)
    ts = now()
    e["event_id"] = e.get("event_id") or event_id_of(e)
    e["first_seen"] = e.get("first_seen") or ts
    e["updated_at"] = ts
    return e


def open_db(path=None):
    path = path or DB_PATH
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE IF NOT EXISTS events ("
        " event_id TEXT PRIMARY KEY, payload TEXT NOT NULL,"
        " disease_en TEXT, country_en TEXT, category TEXT, event_date TEXT,"
        " verification_status TEXT, impact_level TEXT, impact_focus TEXT, updated_at TEXT)"
    )
    cols = {r[1] for r in con.execute("PRAGMA table_info(events)").fetchall()}
    if "record_type" not in cols:
        con.execute("ALTER TABLE events ADD COLUMN record_type TEXT")
    if "impact_level" not in cols:
        con.execute("ALTER TABLE events ADD COLUMN impact_level TEXT")
    if "impact_focus" not in cols:
        con.execute("ALTER TABLE events ADD COLUMN impact_focus TEXT")
    con.execute("CREATE INDEX IF NOT EXISTS idx_events_date ON events(event_date)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_events_status ON events(verification_status)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(record_type)")
    return con


def upsert(con, e, merge=True):
    row = con.execute("SELECT payload FROM events WHERE event_id = ?", (e["event_id"],)).fetchone()
    if row and merge:
        old = json.loads(row[0])
        patch = {k: v for k, v in e.items()
                 if v not in (None, [], {}) and k not in ("event_id", "first_seen")}
        for key, default in DEFAULT_SENTINELS.items():
            if patch.get(key) == default and old.get(key) not in (None, "", default):
                del patch[key]
        e = dict(old, **patch)
        e["event_id"] = old.get("event_id") or e.get("event_id")
        e["first_seen"] = old.get("first_seen") or e.get("first_seen")
        e["updated_at"] = now()
    con.execute(
        "INSERT OR REPLACE INTO events "
        "(event_id,payload,disease_en,country_en,category,event_date,verification_status,impact_level,impact_focus,updated_at,record_type) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (e["event_id"], json.dumps(e, ensure_ascii=False), None,
         e.get("country_en"), "policy", e.get("event_date"),
         e.get("verification_status"), e.get("impact_level"), e.get("impact_focus"), e.get("updated_at"), "policy"),
    )
    con.commit()
    return e


def load_events(con, where="", args=()):
    sql = "SELECT payload FROM events" + (" WHERE " + where if where else "")
    return [json.loads(r[0]) for r in con.execute(sql, args).fetchall()]


def write_event_file(e, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "%s.json" % e["event_id"])
    with open(path, "w", encoding="utf-8") as f:
        json.dump(e, f, ensure_ascii=False, indent=2)
    return path


def _load_input(spec):
    raw = __import__("sys").stdin.read() if spec == "-" else open(spec, encoding="utf-8").read()
    data = json.loads(raw)
    return data.get("events") or [data] if isinstance(data, dict) else data


def main():
    ap = argparse.ArgumentParser(description="政策变化记录标准化入库")
    ap.add_argument("--input", required=True, help="政策 JSON 文件路径; - 为 stdin")
    ap.add_argument("--out", help="政策 JSON 落盘目录")
    ap.add_argument("--db", action="store_true", help="写入 SQLite")
    ap.add_argument("--db-path", help="SQLite 路径覆盖")
    ap.add_argument("--update", action="store_true", help="(已废弃) 合并更新已是默认行为")
    ap.add_argument("--replace", action="store_true",
                    help="整行替换已有政策(默认按 event_id 合并, 空值/默认状态不覆盖已核验、已研判字段)")
    ap.add_argument("--max-age-days", type=int, default=None, metavar="N",
                    help="旧闻拦截: 新记录发布时间(source.publish_date, 缺省 event_date)"
                         "距今天超过 N 天拒收; 默认取环境变量 POLICY_MAX_AGE_DAYS, 未设为 %d;"
                         " 0 关闭(历史回补/库重建重放时用)" % DEFAULT_MAX_AGE_DAYS)
    args = ap.parse_args()
    try:
        records = _load_input(args.input)
    except (OSError, ValueError) as exc:
        print("[错误] 读取/解析输入失败: %s" % exc)
        return 2
    events, failed = [], []
    for i, e in enumerate(records):
        errs = validate(e)
        if errs:
            failed.append((i, errs))
            continue
        events.append((i, fill_defaults(dict(e))))
    for i, errs in failed:
        print("[校验失败] 第%d条: %s" % (i, "; ".join(errs)))
    if not events:
        return 2
    max_age = args.max_age_days
    if max_age is None:
        try:
            max_age = int(os.environ.get("POLICY_MAX_AGE_DAYS", DEFAULT_MAX_AGE_DAYS))
        except ValueError:
            max_age = DEFAULT_MAX_AGE_DAYS
    cutoff = None
    if max_age > 0:
        cutoff = (_date.fromisoformat(biz_today())
                  - datetime.timedelta(days=max_age)).isoformat()
    out_dir = args.out or os.path.join(DATA_DIR, "events", biz_today())
    con = open_db(args.db_path) if (args.db or args.db_path) else None
    # 旧闻拦截只针对新增 event_id: 已入库记录的核验/研判回写走合并更新, 不受窗口限制
    stale, written = [], 0
    for i, e in events:
        reason = stale_reason(e, cutoff)
        if reason and not (con and _event_exists(con, e["event_id"])):
            stale.append((i, reason))
            print("[旧闻拒收] 第%d条: %s  %s @ %s" % (
                i, reason, e.get("title_cn") or e.get("title_en") or "（无标题）",
                e.get("country_en")))
            continue
        if con:
            e = upsert(con, e, merge=not args.replace)
        write_event_file(e, out_dir)
        written += 1
        print("[OK] %s  政策  %s @ %s  %s  [%s] 影响:%s" % (
            e["event_id"], e.get("title_cn") or e.get("title_en") or "（无标题）",
            e.get("country_en"), e.get("event_date"), e.get("verification_status"),
            e.get("impact_level") or "未研判"))
    print("\n入库 %d 条(校验失败 %d 条, 旧闻拒收 %d 条) -> %s%s" % (
        written, len(failed), len(stale), out_dir,
        (" + SQLite: %s" % (args.db_path or DB_PATH)) if con else ""))
    if stale:
        print("提示: 确属新线索或历史回补/库重建时, 用 --max-age-days <N> 放宽窗口, 0 为关闭校验")
    return 1 if (failed or stale) else 0


if __name__ == "__main__":
    raise SystemExit(main())
