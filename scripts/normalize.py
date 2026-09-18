#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""事件标准化入库: 校验字段 -> 生成 event_id -> 写 data/events/ 与 SQLite。

事件字段权威定义见 docs/event-schema.md; 本脚本是该模型的唯一代码实现,
deduplicate.py / risk.py / report.py 都从本模块导入公共函数。

记录分两类(record_type): outbreak=疫情事件(默认) / policy=政策变化事件(policy 分支),
共用同一套存储、去重、核验、研判与日报管线, 仅必填字段与 event_id 派生方式不同。

用法:
  python normalize.py --input events.json --out data/events/2026-09-12 --db
  python normalize.py --input patch.json --update --db    # 按 event_id 合并更新已有事件
  cat raw.json | python normalize.py --input - --db
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

REQUIRED = ["disease_name_cn", "disease_name_en", "category", "country_cn",
            "country_en", "event_date", "source"]
CATEGORIES = {"animal", "plant", "policy"}
VERIF_STATUS = {"verified", "single_source", "unverified", "false_positive", "merged"}
RECORD_TYPES = {"outbreak", "policy"}


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def is_policy(e):
    """政策变化记录: record_type=policy 或 category=policy(两种写法均接受)。"""
    return e.get("record_type") == "policy" or e.get("category") == "policy"


def event_id_of(e):
    if is_policy(e):
        # 政策记录: 同国同动作同政策领域视为一条(日期变更为新动作); id 保持稳定便于修订
        key = "|".join([
            "policy",
            str(e.get("country_en", "")).strip().lower(),
            str(e.get("policy_domain") or e.get("category") or ""),
            str(e.get("action_type") or "").strip().lower(),
            str(e.get("title_en") or e.get("title_cn") or e.get("disease_name_en") or ""),
        ])
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    key = "|".join([
        str(e.get("disease_name_en", "")).strip().lower(),
        str(e.get("country_en", "")).strip().lower(),
        str(e.get("event_date", "")),
        str(e.get("region") or ""),
    ])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def validate(e):
    policy = is_policy(e)
    errs = []
    if policy:
        # 政策记录: 病名可缺(不针对单一病害的政策), 国家 + 日期 + 来源仍必填
        errs += ["缺少必填字段 %s" % k for k in ("country_cn", "country_en", "event_date", "source")
                 if not e.get(k)]
        if not (e.get("title_cn") or e.get("title_en") or e.get("summary_cn")):
            errs.append("政策记录需要 title_cn / title_en / summary_cn 至少其一")
    else:
        errs += ["缺少必填字段 %s" % k for k in REQUIRED if not e.get(k)]
    if e.get("category") not in CATEGORIES:
        errs.append("category 必须为 animal|plant|policy")
    rt = e.get("record_type")
    if rt is not None and rt not in RECORD_TYPES:
        errs.append("record_type 必须为 outbreak|policy")
    src = e.get("source")
    if not isinstance(src, dict) or not src.get("url"):
        errs.append("source.url 必填(可溯源是硬要求)")
    if e.get("verification_status") and e["verification_status"] not in VERIF_STATUS:
        errs.append("verification_status 非法: %s" % e["verification_status"])
    return errs


def fill_defaults(e):
    policy = is_policy(e)
    # 日期粒度: YYYY / YYYY-MM 补齐为当年1月1日 / 当月1日, 并记录精度
    d = str(e["event_date"]).strip()
    precision = e.get("date_precision")
    if len(d) == 4:
        d, precision = d + "-01-01", precision or "year"
    elif len(d) == 7:
        d, precision = d + "-01", precision or "month"
    else:
        precision = precision or "day"
    e["event_date"], e["date_precision"] = d, precision

    e["record_type"] = "policy" if policy else "outbreak"
    if policy:
        e.setdefault("category", "policy")
        e.setdefault("disease_name_cn", None)
        e.setdefault("disease_name_en", None)
    e.setdefault("region", None)
    e.setdefault("host_species", [])
    e.setdefault("quantity", {})
    e.setdefault("cross_sources", [])
    e.setdefault("checked_urls", [])
    e.setdefault("verification_status", "unverified")
    e.setdefault("china_risk", None)
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
        " event_id TEXT PRIMARY KEY,"
        " payload TEXT NOT NULL,"
        " disease_en TEXT, country_en TEXT, category TEXT, event_date TEXT,"
        " verification_status TEXT, risk_level TEXT, focus TEXT, updated_at TEXT)"
    )
    cols = {r[1] for r in con.execute("PRAGMA table_info(events)").fetchall()}
    if "record_type" not in cols:  # 旧库升级: 追加 record_type 列
        con.execute("ALTER TABLE events ADD COLUMN record_type TEXT")
    con.execute("CREATE INDEX IF NOT EXISTS idx_events_date ON events(event_date)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_events_status ON events(verification_status)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(record_type)")
    return con


def upsert(con, e, merge=False):
    """写入一条事件。merge=True 时与库中已有记录合并(新值非空才覆盖)。"""
    row = con.execute("SELECT payload FROM events WHERE event_id = ?", (e["event_id"],)).fetchone()
    if row and merge:
        old = json.loads(row[0])
        patch = {k: v for k, v in e.items() if v not in (None, [], {})}
        e = dict(old, **patch)
        e["updated_at"] = now()
    con.execute(
        "INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (e["event_id"], json.dumps(e, ensure_ascii=False),
         e.get("disease_name_en"), e.get("country_en"), e.get("category"),
         e.get("event_date"), e.get("verification_status"),
         (e.get("china_risk") or {}).get("level"),
         (e.get("china_risk") or {}).get("focus"),
         e.get("updated_at"),
         e.get("record_type", "outbreak")),
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
    raw = __import__("sys").stdin.read() if spec == "-" else \
        open(spec, encoding="utf-8").read()
    data = json.loads(raw)
    if isinstance(data, dict):
        data = data.get("events") or [data]
    return data


def main():
    ap = argparse.ArgumentParser(
        description="疫情事件标准化入库(字段定义见 docs/event-schema.md)",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True, help="JSON 文件路径; - 为 stdin; 事件对象或数组")
    ap.add_argument("--out", help="事件 JSON 落盘目录(默认 data/events/<今天>/)")
    ap.add_argument("--db", action="store_true", help="写入 SQLite(EPIDEMIC_DB, 默认 database/epidemic.db)")
    ap.add_argument("--db-path", help="SQLite 路径覆盖")
    ap.add_argument("--update", action="store_true", help="按 event_id 合并更新已有事件(保留 china_risk 等)")
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
            e = upsert(con, e, merge=args.update)
        write_event_file(e, out_dir)
        risk = e.get("china_risk") or {}
        kind = "政策" if is_policy(e) else "疫情"
        title = e.get("title_cn") or e.get("title_en") if is_policy(e) else e.get("disease_name_en")
        print("[OK] %s  %s  %s @ %s  %s  [%s] 风险:%s/%s" % (
            e["event_id"], kind, title, e.get("country_en"),
            e.get("event_date"), e.get("verification_status"),
            risk.get("level", "-"), risk.get("focus", "-")))

    print("\n入库 %d 条(校验失败 %d 条) -> %s%s" % (
        len(events), len(failed), out_dir,
        (" + SQLite: %s" % (args.db_path or DB_PATH)) if con else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
