#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""疫见全球 · 存量食品类记录批量标记(2026-11-02 新增)

为什么需要它: 2026-11-02 用户明确「本系统只监测**动植物检验检疫**的政策变化,
食品类不需要」。但食品类政策不是从一个口子进来的, 而是三条并行:
  ① ePing 通报面 —— 各国 MRL/农药/饲料通报是 ePing 的高产项, 批量进候选;
  ② 检索面 —— sources.yaml 的 policy_keywords / policy_queries 里有
     "食品添加剂/饲料原料/宠物食品/MRL", 检索式直接去搜它们;
  ③ 抽取面 —— prompts/policy-extraction.md 把"食品/饲料添加剂与原料"列为
     常见政策主题, 抽取 prompt 照着抽。
  三条都已修(见 fetch_eping.py 的 FOOD_TBT_BLOCK、sources.yaml 的词库与检索式、
  prompts/policy-extraction.md 的红线段、scripts/scope_guard.py 的统一判定),
  但**存量**库里已经混入的食品类记录仍留在台账里, 且每天的"今日新增"还会继续报。

本脚本用**同一套判定**(scope_guard.classify_scope, 与 normalize.py 入库端闸门
和 extract_records.py 抽取闸门共用同一份信号表)把存量里的食品类记录标成
`verification_status=false_positive`(误报/非检疫口径) —— **不删除数据**,
可回溯、可撤销。

为什么标记而不删除: 与 flag_non_policy.py 同一处置口径。删除会让历史报告与
推送正文里的 event_id 引用悬空(台账有对外引用), 标记则保留痕迹且日报/推送
立即不再把它们当"今日新增"。误判了用 --unmark 撤回。

用法:
  python3 scripts/flag_food_scope.py                       # 只列候选(默认, 不写库)
  python3 scripts/flag_food_scope.py --apply               # 确认后写入
  python3 scripts/flag_food_scope.py --date 2026-10-31     # 只看某个入库日
  python3 scripts/flag_food_scope.py --unmark <event_id>   # 撤回误标

写入语义: 走 normalize.upsert(merge=True) —— 合并更新, **不覆盖 first_seen**,
不动已有的影响研判结论; 同时把 JSON 存档写回该记录自己的入库日目录。

退出码: 0=成功/预览完成, 2=参数错误或库打不开。
"""
import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from normalize import DATA_DIR, biz_today, open_db, load_events, upsert, write_event_file
import scope_guard


def _mark_note(why):
    return "非检疫口径·食品安全类(2026-11-02 批量标记): %s" % why


def _unmark(event_id, db_path=None):
    """撤回误标: 判定错了时用(恢复 unverified, 清掉备注)。"""
    con = open_db(db_path)
    if con is None:
        print("[错误] 打开数据库失败: %s" % (db_path or "(默认路径)"))
        return 2
    try:
        row = con.execute("SELECT payload FROM events WHERE event_id = ?",
                           (event_id,)).fetchone()
        if not row:
            print("[错误] 找不到 event_id: %s" % event_id)
            return 2
        rec = json.loads(row[0])
        rec["verification_status"] = "unverified"
        rec.pop("verification_notes", None)
        saved = upsert(con, rec, merge=False)
        fs = str(saved.get("first_seen") or biz_today())[:10]
        write_event_file(saved, os.path.join(DATA_DIR, "events", fs))
    finally:
        con.close()
    print("[已撤回] %s 恢复 unverified | %s"
          % (event_id, (saved.get("title_cn") or "")[:60]))
    return 0


def _is_food_scope(e):
    """这条记录是否属于食品类非检疫口径。返回 (是, 原因)。

    判定走 scope_guard.classify_record —— 与抽取端/入库端同一份逻辑、同一份
    信号表(M22 修法后, 商品字段命中强信号会降权放行, 原因带
    scope_guard.DOWNGRADE_PREFIX 前缀, 由 main 分流成"降权放行"清单)。
    """
    is_food, why, _hit = scope_guard.classify_record(e)
    return bool(is_food), why


def main():
    ap = argparse.ArgumentParser(
        description="把存量里的食品类(非检疫口径)记录标记为误报(false_positive)")
    ap.add_argument("--db-path", help="SQLite 路径覆盖")
    ap.add_argument("--date", help="只处理某个入库日(first_seen 的日期, YYYY-MM-DD)")
    ap.add_argument("--apply", action="store_true",
                    help="真正写入(默认只列候选, 便于人工过一眼)")
    ap.add_argument("--unmark", metavar="EVENT_ID",
                    help="撤回某个 event_id 的误标(恢复 unverified)—— 判定错了时用")
    args = ap.parse_args()
    if args.unmark:
        return _unmark(args.unmark, args.db_path)

    try:
        con = open_db(args.db_path)
    except sqlite3.Error as exc:
        # 2026-11-04 修 C13: 库打不开的路径此前是**不可达的死代码** ——
        # open_db 从不返回 None(它会建库/建表), 真打不开时是抛异常, 于是脚本
        # 直接崩成 traceback + 退出码 1, 违背 docstring「退出码 2=库打不开」。
        # 现在把异常接住并按契约返回 2; 顺带修掉该分支里引用未定义变量
        # db_path 的 NameError(应为 args.db_path)。
        print("[错误] 打开数据库失败: %s (%s)" % (args.db_path or "(默认路径)", exc))
        return 2
    if con is None:
        print("[错误] 打开数据库失败: %s" % (args.db_path or "(默认路径)"))
        return 2
    try:
        rows = load_events(con)
    finally:
        con.close()

    hits = []
    downgrades = []
    for e in rows:
        if e.get("record_type") != "policy":
            continue
        # 已标过的跳过: 重复标记没有意义, 且会覆盖原有的误报备注。
        if e.get("verification_status") == "false_positive":
            continue
        fs = str(e.get("first_seen") or "")[:10]
        if args.date and fs != args.date:
            continue
        is_food, why = _is_food_scope(e)
        if is_food:
            hits.append((e, why))
        elif why.startswith(scope_guard.DOWNGRADE_PREFIX):
            # 只在商品清单里命中强信号 → 降权放行(留待人工核验), 不标记。
            downgrades.append((e, why))

    print("扫描 %d 条政策记录, 命中食品类(非检疫口径) %d 条%s"
          % (len(rows), len(hits), "(入库日 %s)" % args.date if args.date else ""))
    for e, why in hits:
        print("  - %s | %s | %s | %s"
              % (e.get("event_id"), str(e.get("first_seen"))[:10], why,
                 (e.get("title_cn") or e.get("title_en") or "")[:44]))
    if downgrades:
        print("\n降权放行 %d 条(仅商品字段命中强信号, 不够定罪, 留待人工核验):" % len(downgrades))
        for e, why in downgrades:
            print("  ~ %s | %s | %s | %s"
                  % (e.get("event_id"), str(e.get("first_seen"))[:10], why,
                     (e.get("title_cn") or e.get("title_en") or "")[:44]))
    if not hits:
        print("无需处理。")
        return 0

    if not args.apply:
        print("\n(仅预览; 加 --apply 执行标记)")
        print("提示: 判定口径在 scripts/scope_guard.py —— 误判时用 --unmark <event_id> 撤回。")
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
    print("\n已标记 %d 条为食品类(非检疫口径)。它们不再进日报与推送卡片的今日新增, "
          "但保留在待核实栏与台账里留痕。" % n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
