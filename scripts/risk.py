#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对华风险结论写回与待办查询。

结论字段与打分方法见 prompts/risk-analysis.md; 校验规则: level/score/focus/rationale 必须合法。

用法:
  python risk.py --list-pending                          # 列出 verified 且缺 china_risk 的事件
  python risk.py --event-id a1b2c3 --level high --score 3.8 --focus 立即关注 \
      --rationale "..." [--trade "..."] [--gacc "..."]   # 便捷写回单条
  python risk.py --from-file risk-batch.json             # 批量写回
    文件格式: {"event_id": ..., "china_risk": {...}} 的对象或其数组
"""
import argparse
import json

from normalize import load_events, now, open_db, upsert

RISK_FIELDS = ["level", "score", "focus", "rationale",
               "trade_relevance", "existing_gacc_measures", "dimension_scores"]
LEVELS = {"high", "medium", "low"}
FOCUS = {"立即关注", "持续观察", "常规记录"}
DIMS = {"commodity", "pathway", "impact", "measures"}
POLICY_DIMS = {"trade", "biosecurity", "response", "alignment"}


def validate_risk(r, record_type="outbreak"):
    allowed_dims = POLICY_DIMS if record_type == "policy" else DIMS
    errs = []
    if r.get("level") not in LEVELS:
        errs.append("level 必须是 high|medium|low")
    if r.get("focus") not in FOCUS:
        errs.append("focus 必须是 立即关注|持续观察|常规记录")
    score = r.get("score")
    if not isinstance(score, (int, float)) or not 0 <= score <= 5:
        errs.append("score 必须在 0-5 之间")
    if not (r.get("rationale") or "").strip():
        errs.append("rationale 必填(必须能看出事实依据)")
    if r.get("dimension_scores") and set(r["dimension_scores"]) - allowed_dims:
        errs.append("dimension_scores 只允许 %s" % sorted(allowed_dims))
    return errs


def write_back(con, event_id, risk):
    rows = load_events(con, "event_id = ?", (event_id,))
    if not rows:
        print("[错误] 事件不存在: %s" % event_id)
        return False
    e = rows[0]
    errs = validate_risk(risk, e.get("record_type") or "outbreak")
    if errs:
        print("[校验失败] %s: %s" % (event_id, "; ".join(errs)))
        return False
    risk = dict(risk)
    risk["scored_at"] = now()
    e["china_risk"] = risk
    e["updated_at"] = now()
    upsert(con, e)
    label = e.get("title_cn") or e.get("title_en") or e.get("disease_name_en") or ""
    print("[OK] %s  %s风险:%s(%s) %s" % (
        event_id, (label + " ") if label else "", risk["level"], risk["score"], risk["focus"]))
    return True


def main():
    ap = argparse.ArgumentParser(description="对华风险结论写回与待办查询")
    ap.add_argument("--list-pending", action="store_true", help="列出 verified 且缺 china_risk 的事件")
    ap.add_argument("--from-file", help="风险结论 JSON 文件")
    ap.add_argument("--event-id", help="便捷写回: 事件 ID")
    ap.add_argument("--level", choices=sorted(LEVELS))
    ap.add_argument("--score", type=float)
    ap.add_argument("--focus", choices=sorted(FOCUS))
    ap.add_argument("--rationale", help="一句话依据(必填)")
    ap.add_argument("--trade", help="trade_relevance: 对华贸易关联")
    ap.add_argument("--gacc", help="existing_gacc_measures: 海关现有措施")
    ap.add_argument("--db-path", help="SQLite 路径覆盖")
    args = ap.parse_args()

    con = open_db(args.db_path)

    if args.list_pending:
        rows = [e for e in load_events(con, "verification_status = 'verified'")
                if not e.get("china_risk")]
        for e in rows:
            label = e.get("title_en") or e.get("disease_name_en") or e.get("summary_cn") or "（无标题）"
            print("%s  %s @ %s %s  %s" % (
                e["event_id"], label, e.get("country_en"),
                e.get("effective_date") or e.get("event_date"), (e.get("summary_cn") or "")[:40]))
        print("\n待研判 %d 条(verified 且无 china_risk)" % len(rows))
        return 0

    if args.from_file:
        with open(args.from_file, encoding="utf-8") as f:
            data = json.load(f)
        items = data if isinstance(data, list) else [data]
        ok = 0
        for item in items:
            risk = item.get("china_risk") or {k: v for k in RISK_FIELDS if k in item}
            if item.get("event_id"):
                risk.setdefault("event_id", item["event_id"])
            event_id = risk.pop("event_id", None)
            if not event_id:
                print("[校验失败] 缺 event_id: %s" % json.dumps(item, ensure_ascii=False)[:80])
                continue
            ok += write_back(con, event_id, risk)
        print("\n写回完成: %d/%d 条成功" % (ok, len(items)))
        return 0 if ok == len(items) else 1

    if args.event_id:
        risk = {"level": args.level, "score": args.score, "focus": args.focus,
                "rationale": args.rationale, "trade_relevance": args.trade,
                "existing_gacc_measures": args.gacc}
        ok = write_back(con, args.event_id, risk)
        return 0 if ok else 1

    ap.error("请提供 --list-pending / --from-file / --event-id 之一")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
