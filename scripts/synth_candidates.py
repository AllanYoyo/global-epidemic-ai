#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从当日存档 meta.json 合成 candidates 清单的确定性兜底(2026-10-22 引入)。

背景: 10-22 三轮实测中, 取数段 Agent 每轮都在"存档完成、写候选清单之前"
耗尽单轮工具调用预算 —— 存档是确定性产物(collect.py), 而候选清单靠
Agent 收尾时一次性写, 预算一断它就没了, 加工段按"清单不存在就跳过"
的指令直接空转出报, 6 条已存档原文一条没进库。

2026-10-24 JSONL 化: 输出从"数组 JSON"改为 candidates.jsonl(每行一条)。
取数段同步改为逐条 append 一行 —— 消灭 "python 读改写整个 JSON" 的
上下文放大(每追加一条就把已增长的整个文件读回一次, 实测是取数段
单轮消耗 65k→200k 的主因之一)。本脚本同时兼容读取旧数组版
candidates.json(历史日期目录)与已有 candidates.jsonl。

本脚本**不产出任何新信息**(不联网、不猜日期、不编造), 只做无损搬运:
  title / url 取自 meta.json(Agent 存档时自己写的);
  source_name 取 URL 域名(确定性提取);
  country_en / publish_date / brief / topics 留空 —— 加工段照
  prompts/policy-extraction.md 从存档原文补齐, 抽取规则本来就要求
  publish_date 必须取自该 URL 自己的页面级日期, 清单里的值只是参考。

已有清单时做**合并**: 按 url 去重, 已有条目原样保留, 只补缺失
的存档条目 —— Agent 已写好的语义字段不会被覆盖。

用法(供 run_scan.sh 在取数段结束后调用, 幂等):
  python scripts/synth_candidates.py                # 默认今天(东八区)
  python scripts/synth_candidates.py --date 2026-10-22
"""
import argparse
import datetime
import json
import os
from urllib.parse import urlsplit

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("EPIDEMIC_DATA_DIR") or os.path.join(REPO, "data")
BIZ_TZ_OFFSET_HOURS = 8  # 与 collect.py / normalize.biz_today 同口径


def biz_now():
    """业务时间(东八区) ISO8601。"""
    return (datetime.datetime.utcnow()
            + datetime.timedelta(hours=BIZ_TZ_OFFSET_HOURS)).isoformat(timespec="seconds")


def domain_of(url):
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def main():
    ap = argparse.ArgumentParser(description="从存档 meta.json 合成/补全当日候选清单(JSONL)")
    ap.add_argument("--date", default=biz_now()[:10], help="业务日期目录, 默认今天(东八区)")
    args = ap.parse_args()

    raw_dir = os.path.join(DATA_DIR, "raw", args.date)
    cand_path = os.path.join(raw_dir, "candidates.jsonl")
    legacy_path = os.path.join(raw_dir, "candidates.json")
    if not os.path.isdir(raw_dir):
        print("[跳过] %s 无存档目录, 无可合成的候选" % raw_dir)
        return 0

    metas = []
    for name in sorted(os.listdir(raw_dir)):
        if not name.endswith(".meta.json"):
            continue
        try:
            with open(os.path.join(raw_dir, name), encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError) as exc:
            print("[WARN] meta.json 解析失败, 跳过: %s (%s)" % (name, exc))
            continue
        url = (meta.get("url") or "").strip()
        if not url:
            continue
        title = (meta.get("title") or "").strip() or url
        metas.append({"title": title, "url": url})

    if not metas:
        print("[跳过] %s 无可用的 meta.json" % raw_dir)
        return 0

    existing, seen = [], set()
    # 兼容读取: 新格式 candidates.jsonl(逐行, 坏行跳过) + 旧数组版 candidates.json
    for path, line_mode in ((cand_path, True), (legacy_path, False)):
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                items = ([json.loads(x) for x in f.read().splitlines() if x.strip()]
                         if line_mode else json.load(f))
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and (item.get("url") or "").strip():
                    u = item["url"].strip()
                    if u not in seen:
                        seen.add(u)
                        existing.append(item)
        except (OSError, ValueError) as exc:
            # 半截文件(预算断在写入中途)是最常见形态: 不让坏文件堵死兜底
            print("[WARN] 已有 %s 解析失败, 忽略其内容: %s"
                  % (os.path.basename(path), exc))

    added = 0
    for m in metas:
        if m["url"] in seen:
            continue
        seen.add(m["url"])
        existing.append({
            "title": m["title"],
            "url": m["url"],
            "country_en": None,
            "publish_date": None,
            "source_name": domain_of(m["url"]) or None,
            "brief": m["title"],
            "topics": [],
        })
        added += 1

    with open(cand_path, "w", encoding="utf-8") as f:
        for item in existing:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    print("[OK] candidates.jsonl: 共 %d 条(存档 %d 条, 本次补 %d 条) -> %s"
          % (len(existing), len(metas), added, cand_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
