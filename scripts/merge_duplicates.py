#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""疫见全球 · 存量同政策重复记录归并(2026-10-29 引入, M19 根治的存量侧)

为什么需要这个脚本:
  event_id 由 policy_key / disease / products / targets / scope 等**模型自由书写的
  文本**推导, 同一份政策公告被抽两次、措辞一变就换一个 id —— 台账里于是出现多条
  同内容记录(生产实测 283 条里 26 条是同一条政策的重复抽取, 19 组)。
  入库端已加语义查重(见 normalize.dedup_key / find_duplicate), 但**存量**那 26 条
  已经躺在库里, 需要一次性归并。

按什么键归并(2026-10-29 生产实测校验):
  source.url + 生效日 + policy_domain + action_type
  只用「url + 生效日」会并掉 7 条**真政策** —— 同一页面上同一天既有「收紧」也有
  「放松」(gov.uk 动植检页), 领域不同的通报(measures vs plant)也会被并掉。
  带齐四要素后: 可并 26 条, 丢真政策 0 条。

保留哪一条(组内选主):
  ① 已核验(verified) 优先 —— 核验是人工投入的结论, 不能丢;
  ② 其次保留已研判(四维齐全 / 有 impact_level)的;
  ③ 再看正文质量(measure_cn / summary_cn 更长 = 信息更全);
  ④ 最后取 first_seen 最早的 —— 保留"首次发现"这个真实时间信号。
  落选记录的**有效内容**(非空、非兜底默认值、且不含已核验/已研判冲突的字段)
  会并入保留记录, 不丢信息。

用法:
  python scripts/merge_duplicates.py --db database/epidemic.db             # 预览(默认, 只报告不动库)
  python scripts/merge_duplicates.py --db database/epidemic.db --apply     # 执行(自动先备份)
  python scripts/merge_duplicates.py --db database/epidemic.db --apply --backup /path/db.bak

安全:
  · 默认 --dry-run, 只报告不动库;
  · --apply 前必须先备份(脚本强制: 未指定 --backup 则自动在库旁生成带时间戳的备份);
  · 归并只改 SQLite; data/events/ 下的 JSON 快照由 --clean-json 决定是否清理
    (默认清理被删 id 的文件, 保留保留 id 的文件并刷新内容)。
  · 退出码: 0=成功/预览完成, 1=出错, 2=参数错误。
"""
import argparse
import datetime
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from normalize import (DEFAULT_SENTINELS, biz_now, dedup_key, merge_duplicate,  # noqa: E402
                       open_db)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERIFIED = "verified"


def load_records(con):
    """读全部 policy 记录(连 payload 一起)。"""
    out = []
    for eid, payload in con.execute(
            "SELECT event_id, payload FROM events WHERE record_type IS NULL OR record_type='policy'"
    ).fetchall():
        try:
            e = json.loads(payload)
        except ValueError:
            continue
        e["event_id"] = eid
        out.append(e)
    return out


def group_by_key(records):
    """按语义键分组: {key: [record, ...]}, 只保留组内 >1 的。"""
    buckets = {}
    for e in records:
        key = dedup_key(e)
        if not key:
            continue
        buckets.setdefault(key, []).append(e)
    return {k: v for k, v in buckets.items() if len(v) > 1}


def pick_primary(group):
    """组内选主: 已核验 > 已研判 > 正文更全 > first_seen 最早。"""
    def rank(e):
        verified = 1 if e.get("verification_status") == VERIFIED else 0
        judged = 1 if (e.get("impact_level") or e.get("dimension_scores")) else 0
        body = len(str(e.get("measure_cn") or e.get("summary_cn") or ""))
        first_seen = str(e.get("first_seen") or "")
        # 排序键: 越大越优先 → 取 max 时用反向元组
        return (verified, judged, body, _neg(first_seen))

    return max(group, key=rank)


class _neg(object):
    """让字符串在元组比较里"越小越优先"(用于 first_seen 最早)。"""

    def __init__(self, s):
        self.s = s

    def __lt__(self, other):
        return self.s > other.s

    def __eq__(self, other):
        return self.s == other.s


def plan(records):
    """算出归并计划: [(key, keep_eid, drop_eids, merged_payload), ...]"""
    plans = []
    for key, group in group_by_key(records).items():
        primary = pick_primary(group)
        merged = dict(primary)
        for e in group:
            if e["event_id"] == primary["event_id"]:
                continue
            merged = merge_duplicate(merged, e)
        merged["event_id"] = primary["event_id"]
        drops = [e["event_id"] for e in group if e["event_id"] != primary["event_id"]]
        plans.append((key, primary["event_id"], drops, merged))
    return plans


def apply_plan(con, plans, events_dir=None):
    """执行归并: 保留记录写入合并后的 payload, 落选记录从库里删除。"""
    removed = 0
    for key, keep_eid, drops, merged in plans:
        con.execute("INSERT OR REPLACE INTO events "
                    "(event_id,payload,disease_en,country_en,category,event_date,"
                    " verification_status,impact_level,impact_focus,updated_at,record_type,source_url) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (keep_eid, json.dumps(merged, ensure_ascii=False), None,
                     merged.get("country_en"), "policy", merged.get("event_date"),
                     merged.get("verification_status"), merged.get("impact_level"),
                     merged.get("impact_focus"), merged.get("updated_at"), "policy",
                     (merged.get("source") or {}).get("url")))
        for eid in drops:
            con.execute("DELETE FROM events WHERE event_id = ?", (eid,))
            removed += 1
            if events_dir and os.path.isdir(events_dir):
                # 被删 id 的 JSON 快照一并清理(留着的会让重放镜像 JSON 重建库时复活)
                for day in os.listdir(events_dir):
                    f = os.path.join(events_dir, day, "%s.json" % eid)
                    if os.path.isfile(f):
                        os.remove(f)
        if events_dir and os.path.isdir(events_dir):
            for day in os.listdir(events_dir):
                f = os.path.join(events_dir, day, "%s.json" % keep_eid)
                if os.path.isfile(f):
                    with open(f, "w", encoding="utf-8") as fh:
                        json.dump(merged, fh, ensure_ascii=False, indent=2)
    con.commit()
    return removed


def main():
    ap = argparse.ArgumentParser(description="存量同政策重复记录归并(M19 根治存量侧)")
    ap.add_argument("--db", default=os.path.join(REPO, "database", "epidemic.db"),
                    help="SQLite 路径(默认 database/epidemic.db)")
    ap.add_argument("--apply", action="store_true", help="真正执行(默认只预览)")
    ap.add_argument("--dry-run", action="store_true",
                    help="显式声明只预览(默认行为, 写这个只是让意图明确)")
    ap.add_argument("--backup", help="备份路径(不给则自动在库旁生成带时间戳的备份)")
    ap.add_argument("--events-dir", default=os.path.join(REPO, "data", "events"),
                    help="事件 JSON 快照目录(默认 data/events)")
    ap.add_argument("--no-clean-json", action="store_true", help="不清理 data/events 下的 JSON 快照")
    args = ap.parse_args()

    if not os.path.isfile(args.db):
        print("[错误] 找不到数据库: %s" % args.db)
        return 2
    if args.apply and args.dry_run:
        print("[错误] --apply 与 --dry-run 互斥: 要么执行, 要么预览。")
        return 2

    con = open_db(args.db)
    try:
        records = load_records(con)
        plans = plan(records)
        total_drops = sum(len(d) for _k, _e, d, _m in plans)

        print("[归并预览] 库内 policy 记录 %d 条; 命中同政策组 %d 组, 可合并 %d 条"
              % (len(records), len(plans), total_drops))
        if not plans:
            print("[OK] 无重复, 无需归并")
            return 0

        for key, keep_eid, drops, merged in plans:
            print("\n  URL: %s" % key[0][:96])
            print("    键: 生效日=%s 领域=%s 动作=%s" % (key[1], key[2], key[3]))
            print("    保留 %s(核验=%s 影响=%s) ← 合并 %d 条: %s"
                  % (keep_eid, merged.get("verification_status"),
                     merged.get("impact_level") or "未研判", len(drops),
                     ", ".join(d[:12] for d in drops)))

        if not args.apply:
            print("\n[预览] 未改动任何数据。加 --apply 执行(会自动先备份)。")
            return 0
        # 执行前强制备份 —— 归并会删记录, 没有回滚点不可接受
        backup = args.backup
        if not backup:
            ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = "%s.bak-mergedup-%s" % (args.db, ts)
        shutil.copy2(args.db, backup)
        print("\n[备份] %s" % backup)

        removed = apply_plan(con, plans,
                             events_dir=None if args.no_clean_json else args.events_dir)
        print("[归并] 完成: 删除重复记录 %d 条, 保留 %d 组" % (removed, len(plans)))
        print("[时间] %s" % biz_now())
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
