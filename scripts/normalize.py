#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""政策变化记录标准化入库。

本分支生产数据合同只有 record_type=policy。字段权威定义见
 docs/event-schema.md; 本脚本负责校验字段、生成稳定 event_id、写 JSON 与 SQLite。
event_id 已存在时默认合并更新: 空值与兜底默认值不覆盖已有核验/研判结论,
需要整行替换时显式传 --replace。
"""
import argparse
import datetime
import hashlib
import json
import os
import sqlite3

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


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


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
        events.append(fill_defaults(dict(e)))
    for i, errs in failed:
        print("[校验失败] 第%d条: %s" % (i, "; ".join(errs)))
    if not events:
        return 2
    out_dir = args.out or os.path.join(DATA_DIR, "events", datetime.date.today().isoformat())
    con = open_db(args.db_path) if (args.db or args.db_path) else None
    for e in events:
        if con:
            e = upsert(con, e, merge=not args.replace)
        write_event_file(e, out_dir)
        print("[OK] %s  政策  %s @ %s  %s  [%s] 影响:%s" % (
            e["event_id"], e.get("title_cn") or e.get("title_en") or "（无标题）",
            e.get("country_en"), e.get("event_date"), e.get("verification_status"),
            e.get("impact_level") or "未研判"))
    print("\n入库 %d 条(校验失败 %d 条) -> %s%s" % (
        len(events), len(failed), out_dir,
        (" + SQLite: %s" % (args.db_path or DB_PATH)) if con else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
