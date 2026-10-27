#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""疫见全球 · 非政策内容批量标记(2026-10-27 新增)

为什么需要它: 2026-10-27 用户指出日报里混进了大量非政策内容(领导出席、会议通知、
表彰新闻、栏目页、装运仪式)。抽取端已加闸门(见 scripts/extract_records.py 的
`screen_non_policy`), 但**存量**库里已经有的那些仍留在台账里, 且每天的"今日新增"
还会继续报它们。本脚本用**同一套判定规则**把存量里的非政策记录标成
`verification_status=false_positive`(误报/非政策) —— 不删除数据, 可回溯、可撤销。

口径与抽取端一致(同一份 `screen_non_policy`), 不另立一套标准。

用法:
  python3 scripts/flag_non_policy.py                       # 只列候选(默认, 不写库)
  python3 scripts/flag_non_policy.py --apply               # 确认后写入
  python3 scripts/flag_non_policy.py --date 2026-10-27     # 只看某个入库日
  python3 scripts/flag_non_policy.py --apply --db-path xxx # 指定库

写入语义: 走 normalize.upsert(merge=True) —— 合并更新, **不覆盖 first_seen**,
不动已有的影响研判结论; 同时把 JSON 存档写回该记录自己的入库日目录。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from normalize import DATA_DIR, biz_today, open_db, load_events, upsert, write_event_file
from extract_records import screen_non_policy


def _mark_note(why):
    return "非政策内容(2026-10-27 批量标记): %s" % why


def main():
    ap = argparse.ArgumentParser(description="把存量里的非政策内容标记为误报(false_positive)")
    ap.add_argument("--db-path", help="SQLite 路径覆盖")
    ap.add_argument("--date", help="只处理某个入库日(first_seen 的日期, YYYY-MM-DD)")
    ap.add_argument("--apply", action="store_true",
                    help="真正写入(默认只列候选, 便于人工过一眼)")
    args = ap.parse_args()
    con = open_db(args.db_path)
    if con is None:
        print("[错误] 打开数据库失败: %s" % (args.db_path or "(默认路径)"))
        return 2
    try:
        rows = load_events(con)
    finally:
        con.close()
    hits = []
    for e in rows:
        if e.get("record_type") != "policy":
            continue
        if e.get("verification_status") == "false_positive":
            continue
        fs = str(e.get("first_seen") or "")[:10]
        if args.date and fs != args.date:
            continue
        reject, why = screen_non_policy(e)
        if reject:
            hits.append((e, why))
    print("扫描 %d 条政策记录, 命中非政策内容 %d 条%s"
          % (len(rows), len(hits), "(入库日 %s)" % args.date if args.date else ""))
    for e, why in hits:
        print("  - %s | %s | %s | %s"
              % (e.get("event_id"), str(e.get("first_seen"))[:10], why,
                 (e.get("title_cn") or e.get("title_en") or "")[:44]))
    if not hits:
        print("无需处理。")
        return 0
    if not args.apply:
        print("\n(仅预览; 加 --apply 执行标记)")
        return 0
    con = open_db(args.db_path)
    n = 0
    try:
        for e, why in hits:
            e2 = dict(e)
            e2["verification_status"] = "false_positive"
            e2["verification_notes"] = _mark_note(why)
            saved = upsert(con, e2, merge=True)
            fs = str(saved.get("first_seen") or biz_today())[:10]
            write_event_file(saved, os.path.join(DATA_DIR, "events", fs))
            n += 1
            print("[已标记] %s false_positive | %s"
                  % (saved.get("event_id"), (saved.get("title_cn") or "")[:44]))
    finally:
        con.close()
    print("\n已标记 %d 条为误报(非政策内容)。它们不再进日报与推送卡片的今日新增, "
          "但保留在待核实栏与台账里留痕。" % n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
