#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""疫情事件去重合并。

聚类规则: 同 category + disease_en + country_en, 且 event_date 在时间窗内(默认7天)、
地区相近(完全相同/编辑相似度>=0.7/双方均为空)的事件合为一簇;
保留"信息最全 + 来源最权威 + 核验等级最高"的一条, 其余标记 merged 并把来源并入主事件。

用法:
  python deduplicate.py --dry-run   # 只看合并计划, 不写库
  python deduplicate.py             # 执行合并(写 SQLite + 回写 data/events/)
"""
import argparse
import datetime
import difflib
import json
import os
from collections import defaultdict

from normalize import DATA_DIR, load_events, now, open_db, upsert, write_event_file

VERIF_RANK = {"verified": 3, "single_source": 2, "unverified": 1, "false_positive": 0, "merged": -1}
TIER_RANK = {1: 3, 2: 2, 3: 1}
REGION_SIMILARITY = 0.7


def _date(s):
    return datetime.date.fromisoformat(str(s)[:10])


def region_similar(a, b):
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    if not a or not b:
        return a == b  # 一方缺地区时, 仅当双方都缺才视为同地, 避免跨省误并
    return difflib.SequenceMatcher(None, a, b).ratio() >= REGION_SIMILARITY


def primary_score(e):
    return (
        VERIF_RANK.get(e.get("verification_status"), 0),
        TIER_RANK.get((e.get("source") or {}).get("tier"), 0),
        len(json.dumps(e, ensure_ascii=False)),
    )


def cluster(events, days):
    groups = defaultdict(list)
    for e in events:
        key = (e.get("category"), (e.get("disease_name_en") or "").lower(),
               (e.get("country_en") or "").lower())
        groups[key].append(e)
    clusters = []
    for members in groups.values():
        members.sort(key=lambda x: str(x.get("event_date")))
        used = [False] * len(members)
        for i, seed in enumerate(members):
            if used[i]:
                continue
            c = [seed]
            used[i] = True
            for j in range(i + 1, len(members)):
                if used[j]:
                    continue
                other = members[j]
                try:
                    gap = abs((_date(seed["event_date"]) - _date(other["event_date"])).days)
                except (KeyError, ValueError):
                    continue
                if gap <= days and region_similar(seed.get("region"), other.get("region")):
                    c.append(other)
                    used[j] = True
            clusters.append(c)
    return clusters


def merge_into(primary, dup):
    """把重复事件的来源与缺失字段并入主事件(就地修改并返回)。"""
    seen_urls = {(primary.get("source") or {}).get("url")}
    pool = list(primary.get("cross_sources") or []) + \
        [dup.get("source")] + list(dup.get("cross_sources") or [])
    for s in pool:
        if isinstance(s, dict) and s.get("url") and s["url"] not in seen_urls:
            seen_urls.add(s["url"])
            primary.setdefault("cross_sources", []).append(s)
    for k, v in dup.items():
        if k in ("event_id", "first_seen", "source", "cross_sources", "verification_status",
                 "verification_notes", "checked_urls", "updated_at"):
            continue
        if primary.get(k) in (None, [], {}) and v not in (None, [], {}):
            primary[k] = v
    note = "已并入事件 %s(来源: %s)" % (dup["event_id"], (dup.get("source") or {}).get("name"))
    primary["verification_notes"] = \
        ((primary.get("verification_notes") or "") + "; " + note).strip("; ")
    primary["updated_at"] = now()
    return primary


def main():
    ap = argparse.ArgumentParser(description="疫情事件去重合并(同病+同国+时间窗+地区相近)")
    ap.add_argument("--days", type=int, default=7, help="同簇事件日期窗(天), 默认 7")
    ap.add_argument("--dry-run", action="store_true", help="只打印合并计划, 不写库")
    ap.add_argument("--db-path", help="SQLite 路径覆盖")
    args = ap.parse_args()

    con = open_db(args.db_path)
    events = [e for e in load_events(con) if e.get("verification_status") != "merged"]
    clusters = cluster(events, args.days)

    plan, merged_count = [], 0
    for c in clusters:
        if len(c) < 2:
            continue
        c.sort(key=primary_score, reverse=True)
        primary, dups = c[0], c[1:]
        plan.append((primary, dups))
        print("簇: %s @ %s(%s)  保留 %s, 并入 %s" % (
            primary.get("disease_name_en"), primary.get("country_en"),
            primary.get("category"), primary["event_id"],
            ", ".join(d["event_id"] for d in dups)))

    if args.dry_run:
        print("\n[dry-run] 共 %d 簇可合并, 未写入。去掉 --dry-run 执行。" % len(plan))
        return 0

    for primary, dups in plan:
        for d in dups:
            dup = dict(d)
            primary = merge_into(primary, dup)
            dup["verification_status"] = "merged"
            dup["merged_into"] = primary["event_id"]
            dup["updated_at"] = now()
            upsert(con, dup)
            write_event_file(dup, os.path.join(DATA_DIR, "events", str(dup.get("event_date"))))
        upsert(con, primary)
        write_event_file(primary, os.path.join(DATA_DIR, "events", str(primary.get("event_date"))))
        merged_count += len(dups)

    print("\n完成: %d 簇, 合并 %d 条重复事件。" % (len(plan), merged_count))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
