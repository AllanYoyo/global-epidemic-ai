#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外国政府政策变化的对华影响研判写回工具。

四维加权合同与 prompts/policy-impact.md 一致, 并由本脚本强制校验:
  impact_score = trade*0.35 + biosecurity*0.30 + response*0.25 + alignment*0.10
  score >= 3.5 -> 高影响 / >= 2.0 -> 中影响 / 其余 -> 低影响
  高影响 -> 立即关注, 中影响 -> 持续观察, 低影响 -> 常规记录
四维齐全时 impact_score/impact_level/impact_focus 可省略, 由代码推导。
"""
import argparse
import json
from normalize import load_events, now, open_db, upsert

IMPACT_TYPES = {"约束", "机会", "中性"}
IMPACT_LEVELS = {"高影响", "中影响", "低影响"}
CHINA_RELEVANCE = {"直接涉及中国", "间接影响", "暂无明显关联"}
FOCUS = {"立即关注", "持续观察", "常规记录"}
DIMS = {"trade", "biosecurity", "response", "alignment"}
WEIGHTS = {"trade": 0.35, "biosecurity": 0.30, "response": 0.25, "alignment": 0.10}
FOCUS_BY_LEVEL = {"高影响": "立即关注", "中影响": "持续观察", "低影响": "常规记录"}


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def dims_complete(dims):
    return all(k in dims for k in WEIGHTS)


def dims_valid(dims):
    return all(_num(v) and 0 <= v <= 5 for v in dims.values())


def weighted_score(dims):
    return round(sum(WEIGHTS[k] * float(dims[k]) for k in WEIGHTS), 2)


def level_of(score):
    return "高影响" if score >= 3.5 else ("中影响" if score >= 2.0 else "低影响")


def derive_missing(r):
    """按数据合同补全可推导字段: 四维 -> 加权分 -> 等级 -> 关注档。"""
    r.setdefault("dimension_scores", {})
    dims = r["dimension_scores"]
    if dims_complete(dims) and dims_valid(dims) and r.get("impact_score") is None:
        r["impact_score"] = weighted_score(dims)
    if r.get("impact_level") is None and _num(r.get("impact_score")):
        r["impact_level"] = level_of(r["impact_score"])
    if r.get("impact_focus") is None and r.get("impact_level") in FOCUS_BY_LEVEL:
        r["impact_focus"] = FOCUS_BY_LEVEL[r["impact_level"]]
    return r


def validate_result(r):
    errors = []
    if r.get("impact_type") not in IMPACT_TYPES: errors.append("impact_type 必须为 约束|机会|中性")
    if r.get("impact_level") not in IMPACT_LEVELS: errors.append("impact_level 必须为 高影响|中影响|低影响")
    if r.get("china_relevance") not in CHINA_RELEVANCE: errors.append("china_relevance 取值不合法")
    if not (r.get("recommended_action") or "").strip(): errors.append("recommended_action 必填")
    if not (r.get("impact_rationale") or "").strip(): errors.append("impact_rationale 必填")
    score = r.get("impact_score")
    if not _num(score) or not 0 <= score <= 5:
        errors.append("impact_score 必须在 0-5 之间")
    elif r.get("impact_level") != level_of(score):
        errors.append("impact_level 应为 %s(impact_score=%s)" % (level_of(score), score))
    if r.get("impact_focus") not in FOCUS: errors.append("impact_focus 必须为 立即关注|持续观察|常规记录")
    elif r.get("impact_level") in FOCUS_BY_LEVEL and r["impact_focus"] != FOCUS_BY_LEVEL[r["impact_level"]]:
        errors.append("impact_focus 应为 %s(与 %s 对应)" % (FOCUS_BY_LEVEL[r["impact_level"]], r["impact_level"]))
    dims = r.get("dimension_scores") or {}
    if set(dims) - DIMS: errors.append("dimension_scores 只允许 %s" % sorted(DIMS))
    for k, v in dims.items():
        if not _num(v) or not 0 <= v <= 5:
            errors.append("dimension_scores.%s 必须是 0-5 的数值" % k)
    if dims_complete(dims) and dims_valid(dims) and _num(score):
        expected = weighted_score(dims)
        if abs(float(score) - expected) > 0.05:
            errors.append("impact_score=%s 与四维加权结果 %s 不一致" % (score, expected))
    return errors


def write_back(con, event_id, result):
    rows = load_events(con, "event_id = ?", (event_id,))
    if not rows:
        print("[错误] 政策记录不存在: %s" % event_id); return False
    policy = rows[0]; result = derive_missing(dict(result)); errors = validate_result(result)
    if errors:
        print("[校验失败] %s: %s" % (event_id, "; ".join(errors))); return False
    for key in ("impact_type", "impact_level", "china_relevance", "recommended_action",
                "impact_score", "impact_focus", "impact_rationale", "dimension_scores"):
        policy[key] = result[key]
    policy["updated_at"] = now(); upsert(con, policy)
    print("[OK] %s  %s/%s  %s" % (event_id, policy["impact_type"], policy["impact_level"], policy["recommended_action"]))
    return True


def parse_dims(pairs):
    dims = {}
    for item in pairs:
        for pair in item.split(","):
            key, _, value = pair.partition("=")
            key, value = key.strip(), value.strip()
            if not key:
                continue
            try:
                dims[key] = float(value)
            except ValueError:
                raise SystemExit("[错误] --dim 格式应为 key=数字: %s" % pair)
    return dims


def main():
    ap = argparse.ArgumentParser(description="写回政策对华影响研判")
    ap.add_argument("--list-pending", action="store_true", help="列出 verified 且缺 impact_level 的政策")
    ap.add_argument("--from-file", help="政策影响 JSON 文件")
    ap.add_argument("--event-id", help="政策记录 ID")
    ap.add_argument("--impact-type", choices=sorted(IMPACT_TYPES)); ap.add_argument("--impact-level", choices=sorted(IMPACT_LEVELS))
    ap.add_argument("--china-relevance", choices=sorted(CHINA_RELEVANCE)); ap.add_argument("--action", dest="recommended_action")
    ap.add_argument("--rationale", dest="impact_rationale"); ap.add_argument("--score", dest="impact_score", type=float)
    ap.add_argument("--focus", dest="impact_focus", choices=sorted(FOCUS)); ap.add_argument("--db-path")
    ap.add_argument("--dim", action="append", default=[], metavar="KEY=V",
                    help="维度分数 0-5, 可多次或逗号分隔: --dim trade=4 --dim biosecurity=3,response=3;"
                         " 四维齐全时 score/level/focus 可省略由代码推导")
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
                  "impact_focus": args.impact_focus, "dimension_scores": parse_dims(args.dim)}
        return 0 if write_back(con, args.event_id, result) else 1
    ap.error("请提供 --list-pending / --from-file / --event-id 之一")


if __name__ == "__main__": raise SystemExit(main())
