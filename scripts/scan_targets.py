#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""渲染「当日应扫国别与源清单」的紧凑文本, 供 run_scan.sh 注入取数段 prompt。

为什么需要它(2026-10-24 token 体检): 取数段 prompt 原先让 Agent 自己
read_file config/sources.yaml + SKILL.md —— 实测 Agent 临场读了 58KB 的
AGENTS.md/SKILL.md/scripts, 这些内容全量进上下文且逐轮重发, 是单轮消耗从
65k 涨到 200k+ 的主因之一。改为由本脚本在跑批前把**当天相关**的国别/源
入口确定性渲染成 ~30 行文本, Agent 不再需要读任何仓库文件。

受限解析(仓库不依赖 PyYAML, stdlib 优先), 只认 sources.yaml 的既有书写格式:
  · 源块:   两空格缩进的 "- id: xxx" 起, 块内 name/url/tier/track 字段;
  · 国别条目: 四空格缩进的单行 flow "- {cn: .., en: .., source_ids: [..](, shard: ..)}";
  · 档位轮转: weekly_shards 段 "  N: {desc: .., scan: [档名..]}"。
格式漂移时输出会缺行(fail-visible, 见末尾自检), 不会编造。

用法:
  python scripts/scan_targets.py                     # 今天(东八区)
  python scripts/scan_targets.py --date 2026-10-24
"""
import argparse
import datetime
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES_PATH = os.path.join(REPO, "config", "sources.yaml")
BIZ_TZ_OFFSET_HOURS = 8  # 与 normalize.biz_today 同口径

# 必扫档: 不参与周轮转, 每天都要扫
ALWAYS_TIERS = ("priority_1", "regional", "priority_2")


def biz_today():
    return (datetime.datetime.utcnow()
            + datetime.timedelta(hours=BIZ_TZ_OFFSET_HOURS)).date().isoformat()


def _val(s):
    return s.strip().strip('"').strip("'")


def parse_sources(text):
    """源表: id -> {name, url, tier, track}。只认两空格缩进的 '- id:' 块。"""
    sources = {}
    cur = None
    for line in text.splitlines():
        m = re.match(r"^  - id: (\S+)\s*$", line)
        if m:
            cur = m.group(1)
            sources[cur] = {}
            continue
        if cur is None:
            continue
        m = re.match(r"^    (name|url|tier|track): (.*)$", line)
        if m:
            sources[cur][m.group(1)] = _val(m.group(2))
        elif line.startswith(("  - ", "  #", "  \n")) or re.match(r"^\S", line):
            cur = None  # 块结束(下一个源/注释/顶层 key)
    return sources


def parse_weekly_shards(text):
    """周轮转: ISO 星期(int) -> 当天要扫的档位名列表。"""
    shards = {}
    in_seg = False
    for line in text.splitlines():
        if re.match(r"^weekly_shards:\s*$", line):
            in_seg = True
            continue
        if in_seg:
            if line and not line[0].isspace():
                break
            m = re.match(r"^  (\d):\s*\{desc: [^,]+, scan: \[([^\]]*)\]\}", line)
            if m:
                tiers = [t.strip() for t in m.group(2).split(",") if t.strip()]
                shards[int(m.group(1))] = tiers
    return shards


def parse_watch_countries(text):
    """国别主干: 档位名 -> [{cn, en, source_ids, shard}]。"""
    tiers = {}
    cur_tier = None
    in_seg = False
    for line in text.splitlines():
        if re.match(r"^policy_watch_countries:\s*$", line):
            in_seg = True
            continue
        if not in_seg:
            continue
        if line and not line[0].isspace():
            break
        if line.lstrip().startswith("#"):
            continue
        m = re.match(r"^  ([a-z_0-9]+):\s*(?:\#.*)?$", line)
        if m:
            cur_tier = m.group(1)
            tiers.setdefault(cur_tier, [])
            continue
        m = re.match(r"^    - \{(.+)\}\s*$", line)
        if m and cur_tier:
            body = m.group(1)
            g = lambda k: (re.search(r"%s: ([^,}]+(?:\[[^\]]*\])?)" % k, body) or [None, None])
            cn = re.search(r"cn: ([^,}]+)", body)
            en = re.search(r"en: ([^,}]+)", body)
            ids = re.search(r"source_ids: \[([^\]]*)\]", body)
            shard = re.search(r"shard: \"([^\"]*)\"", body)
            if cn and ids:
                tiers[cur_tier].append({
                    "cn": _val(cn.group(1)),
                    "en": _val(en.group(1)) if en else "",
                    "source_ids": [s.strip() for s in ids.group(1).split(",") if s.strip()],
                    "shard": shard.group(1) if shard else "",
                })
    return tiers


def render(date_str):
    with open(SOURCES_PATH, encoding="utf-8") as f:
        text = f.read()
    sources = parse_sources(text)
    weekly = parse_weekly_shards(text)
    tiers = parse_watch_countries(text)

    d = datetime.date.fromisoformat(date_str)
    iso = d.isoweekday()
    tier_today = list(ALWAYS_TIERS) + weekly.get(iso, [])

    lines = []
    n_sel = 0
    for tier in tier_today:
        picked = []
        for c in tiers.get(tier, []):
            if c["shard"] and str(iso) not in c["shard"].split():
                continue
            picked.append(c)
        if not picked:
            continue
        lines.append("[%s]" % tier)
        for c in picked:
            for sid in c["source_ids"]:
                src = sources.get(sid) or {}
                if src.get("track") not in (None, "policy"):
                    continue
                url = src.get("url") or "(无入口URL, 用官方站检索)"
                lines.append("%s %s | %s %s" % (c["cn"], c["en"], sid, url))
                n_sel += 1
    header = "今日=%s(%s ISO=%d) 档位: %s" % (
        date_str, "周" + "一二三四五六日"[iso - 1], iso, "/".join(tier_today))
    out = header + "\n" + "\n".join(lines)

    # fail-visible 自检: 正常一天至少 15 行(必扫档 3 档 * 多国); 明显偏少说明
    # sources.yaml 书写格式漂移了, 解析器没吃到 —— 宁可让跑批日志大喊, 也不静默空扫。
    if n_sel < 10:
        out += "\n[WARN] 仅解析到 %d 个国别-源对, 疑似 sources.yaml 格式漂移, 请人工核查!" % n_sel
    return out, n_sel


def main():
    ap = argparse.ArgumentParser(description="渲染当日应扫国别/源清单(注入取数 prompt)")
    ap.add_argument("--date", default=biz_today(), help="业务日期(东八区), 默认今天")
    args = ap.parse_args()
    try:
        out, n = render(args.date)
    except (OSError, ValueError) as exc:
        print("[错误] 渲染失败: %s" % exc)
        return 1
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
