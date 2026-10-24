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

分组(2026-10-24 P1 引入, 取数段拆小促收敛): 每轮都打满 max_turns 的实证
(10-24 两轮 90/90、两轮 50/50)说明单作业装不下全部当日通道。按档位拆两组:
  组1 = 必扫档(priority_1/regional/priority_2), 组2 = 当日轮转档。
run_scan.sh 逐组发起取数(独立超时、独立断点), 单组打满的爆炸半径从"整轮
作废"缩到"单组作废"。

用法:
  python scripts/scan_targets.py                     # 今天(东八区), 全部当日通道(兼容旧口径)
  python scripts/scan_targets.py --date 2026-10-24
  python scripts/scan_targets.py --group 1           # 只渲染必扫档组
  python scripts/scan_targets.py --groups            # 只打印各组源对数(机器可读, 供 run_scan.sh 解析)
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


def _pick(date_str, group=None):
    """选定当日的 (档位, 国别cn, 国别en, 源id, 入口URL) 列表。

    group=None 全部; 1=必扫档; 2=当日轮转档。与 render 共用同一选择口径。
    """
    with open(SOURCES_PATH, encoding="utf-8") as f:
        text = f.read()
    sources = parse_sources(text)
    weekly = parse_weekly_shards(text)
    tiers = parse_watch_countries(text)

    d = datetime.date.fromisoformat(date_str)
    iso = d.isoweekday()
    tier_today = list(ALWAYS_TIERS) + weekly.get(iso, [])

    picked = []
    for tier in tier_today:
        if group == 1 and tier not in ALWAYS_TIERS:
            continue
        if group == 2 and tier in ALWAYS_TIERS:
            continue
        for c in tiers.get(tier, []):
            if c["shard"] and str(iso) not in c["shard"].split():
                continue
            for sid in c["source_ids"]:
                src = sources.get(sid) or {}
                if src.get("track") not in (None, "policy"):
                    continue
                url = src.get("url") or "(无入口URL, 用官方站检索)"
                picked.append((tier, c["cn"], c["en"], sid, url))
    return picked


def groups_info(date_str):
    """各组源对数: {1: n1, 2: n2}(供 run_scan.sh 决定拆几组、怎么分超时)。"""
    info = {}
    for g in (1, 2):
        info[g] = len(_pick(date_str, g))
    return info


def render(date_str, group=None):
    picked = _pick(date_str, group)

    lines = []
    cur_tier = None
    for tier, cn, en, sid, url in picked:
        if tier != cur_tier:
            lines.append("[%s]" % tier)
            cur_tier = tier
        lines.append("%s %s | %s %s" % (cn, en, sid, url))
    n_sel = len(picked)
    header = "今日=%s(%s) 本组档位: %s" % (
        date_str, "周" + "一二三四五六日"[datetime.date.fromisoformat(date_str).isoweekday() - 1],
        "/".join(dict.fromkeys(t for t, *_ in picked)) or "(空)")
    out = header + "\n" + "\n".join(lines)

    # fail-visible 自检: 全量口径正常一天至少 10 行(必扫档 3 档 * 多国); 单组
    # 口径天然可能少于 10(轮转档小的日子), 只对"一组源对数为 0"示警。明显偏少
    # 说明 sources.yaml 书写格式漂移了, 解析器没吃到 —— 宁可让跑批日志大喊,
    # 也不静默空扫。
    if group is None and n_sel < 10:
        out += "\n[WARN] 仅解析到 %d 个国别-源对, 疑似 sources.yaml 格式漂移, 请人工核查!" % n_sel
    if group is not None and n_sel == 0:
        out += "\n[WARN] 本组(组%d)当日 0 个源对 —— 若今日轮转档确有条目, 疑似格式漂移!" % group
    return out, n_sel


def main():
    ap = argparse.ArgumentParser(description="渲染当日应扫国别/源清单(注入取数 prompt)")
    ap.add_argument("--date", default=biz_today(), help="业务日期(东八区), 默认今天")
    ap.add_argument("--group", type=int, choices=(1, 2), default=None,
                    help="只渲染该组(1=必扫档, 2=当日轮转档); 缺省=全部")
    ap.add_argument("--groups", action="store_true",
                    help="只打印各组源对数(机器可读): n1=.. n2=..")
    args = ap.parse_args()
    try:
        if args.groups:
            info = groups_info(args.date)
            print("n1=%d n2=%d" % (info[1], info[2]))
            return 0
        out, n = render(args.date, args.group)
    except (OSError, ValueError) as exc:
        print("[错误] 渲染失败: %s" % exc)
        return 1
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
