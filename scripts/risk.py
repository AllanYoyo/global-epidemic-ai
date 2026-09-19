#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外国政府政策变化的对华影响研判写回工具。"""
import argparse
import json
from normalize import load_events, now, open_db, upsert

IMPACT_TYPES = {"约束", "机会", "中性"}
IMPACT_LEVELS = {"高影响", "中影响", "低影响"}
CHINA_RELEVANCE = {"直接涉及中国", "间接影响", "暂无明显关联"}
FOCUS = {"立即关注", "持续观察", "常规记录"}
DIMS = {"trade", "biosecurity", "response", "alignment"}


def validate_result(r):
    errors = []
    if r.get("impact_type") not in IMPACT_TYPES: errors.append("impact_type 必须为 约束|机会|中性")
    if r.get("impact_level") not in IMPACT_LEVELS: errors.append("impact_level 必须为 高影响|中影响|低影响")
    if r.get("china_relevance") not in CHINA_RELEVANCE: errors.append("china_relevance 取值不合法")
    if not (r.get("recommended_action") or "").strip(): errors.append("recommended_action 必填")
    if not (r.get("impact_rationale") or "").strip(): errors.append("impact_rationale 必填")
    if r.get("impact_focus") not in FOCUS: errors.append("impact_focus 必须为 立即关注|持续观察|常规记录")
    if not isinstance(r.get("impact_score"), (int, float)) or not 0 <= r["impact_score"] <= 5:
        errors.append("impact_score 必须在 0-5 之间")
    if set(r.get("dimension_scores") or {}) - DIMS: errors.append("dimension_scores 只允许 %s" % sorted(DIMS))
    return errors


def write_back(con, event_id, result):
    rows = load_events(con, "event_id = ?", (event_id,))
    if not rows:
        print("[错误] 政策记录不存在: %s" % event_id); return False
    policy = rows[0]; errors = validate_result(result)
    if errors:
        print("[校验失败] %s: %s" % (event_id, "; ".join(errors))); return False
    for key in ("impact_type", "impact_level", "china_relevance", "recommended_action",
                "impact_score", "impact_focus", "impact_rationale", "dimension_scores"):
        policy[key] = result[key]
    policy["updated_at"] = now(); upsert(con, policy)
    print("[OK] %s  %s/%s  %s" % (event_id, policy["impact_type"], policy["impact_level"], policy["recommended_action"]))
    return True


def main():
    ap = argparse.ArgumentParser(description="写回政策对华影响研判")
    ap.add_argument("--list-pending", action="store_true", help="列出 verified 且缺 impact_level 的政策")
    ap.add_argument("--from-file", help="政策影响 JSON 文件")
    ap.add_argument("--event-id", help="政策记录 ID")
    ap.add_argument("--impact-type", choices=sorted(IMPACT_TYPES)); ap.add_argument("--impact-level", choices=sorted(IMPACT_LEVELS))
    ap.add_argument("--china-relevance", choices=sorted(CHINA_RELEVANCE)); ap.add_argument("--action", dest="recommended_action")
    ap.add_argument("--rationale", dest="impact_rationale"); ap.add_argument("--score", dest="impact_score", type=float)
    ap.add_argument("--focus", dest="impact_focus", choices=sorted(FOCUS)); ap.add_argument("--db-path")
    args = ap.parse_args(); con = open_db(args.db_path)
    if args.list_pending:
        rows = [e for e in load_events(con, "verification_status = 'verified'") if not e.get("impact_level")]
        for e in rows: print("%s  %s @ %s %s" % (e["event_id"], e.get("title_en") or e.get("title_cn"), e.get("country_en"), e.get("effective_date") or e.get("event_date")))
        print("\n待影响研判 %d 条" % len(rows)); return 0
    if args.from_file:
        with open(args.from_file, encoding="utf-8") as f: data = json.load(f)
        items = data if isinstance(data, list) else [data]; ok = 0
        for item in items:
            result = dict(item.get("policy_impact") or item); event_id = result.pop("event_id", None) or item.get("event_id")
            ok += bool(event_id and write_back(con, event_id, result))
        print("\n写回完成: %d/%d 条成功" % (ok, len(items))); return 0 if ok == len(items) else 1
    if args.event_id:
        result = {"impact_type": args.impact_type, "impact_level": args.impact_level,
                  "china_relevance": args.china_relevance, "recommended_action": args.recommended_action,
                  "impact_rationale": args.impact_rationale, "impact_score": args.impact_score,
                  "impact_focus": args.impact_focus, "dimension_scores": {}}
        return 0 if write_back(con, args.event_id, result) else 1
    ap.error("请提供 --list-pending / --from-file / --event-id 之一")


if __name__ == "__main__": raise SystemExit(main())
