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

分组(2026-10-24 P1 引入, 2026-10-25 晨跑再修): 取数单作业不收敛的实证
(10-24 两轮 90/90、两轮 50/50 打满; 10-25 必扫 21 源对单组 50 轮打满且
Agent 把候选囤在对话里 0 交付)。当日全部源对(必扫+轮转, 按固定优先级序)
按 GROUP_CHUNK 个一块切成 K 块, run_scan.sh 逐块发起取数(独立超时、独立
断点), 单块打满只废单块。10-25 实测约 4-5 轮/源 → 6 对/块 ≈ 30 轮内收敛。

用法:
  python scripts/scan_targets.py                     # 今天(东八区), 全部当日通道(兼容旧口径)
  python scripts/scan_targets.py --date 2026-10-24
  python scripts/scan_targets.py --group 1           # 只渲染第 1 块
  python scripts/scan_targets.py --groups            # 只打印各块源对数(机器可读, 供 run_scan.sh 解析)
"""
import argparse
import datetime
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES_PATH = os.path.join(REPO, "config", "sources.yaml")
BIZ_TZ_OFFSET_HOURS = 8  # 与 normalize.biz_today 同口径

# 每日全扫(2026-10-25 起): 全部档位每天都进清单。P4 确定性取数(curl, 零 LLM)
# 后, 源数不再消耗 token —— 抽取成本只跟存档数走(每日 ≤12 硬上限), 周轮转
# 分片失去省 token 的存在前提。此前 10-24 引入本脚本时 ALWAYS_TIERS 只列了
# 3 个必扫档, 而 weekly_shards 周一~五/周日 scan 全空 —— border_neighbours/
# export_partners 两档 23 个源**没有任何调度日**(俄/越/哈/蒙等 10-14 特意
# 标注"高疫区邻国每日扫"的源全部失明), 属配置断裂, 见 known-issues H17。
ALWAYS_TIERS = ("priority_1", "regional", "priority_2",
                "border_neighbours", "export_partners", "state_level")

# 每块源对上限(2026-10-25): 21 对单组 50 轮打满 → 按 6 对/块切
GROUP_CHUNK = 6


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

    group=None 全部(按固定优先级序); group=K(≥1) 只取第 K 块
    (按 GROUP_CHUNK 个源对一块顺序切, 块数随当日源对数浮动)。
    """
    with open(SOURCES_PATH, encoding="utf-8") as f:
        text = f.read()
    sources = parse_sources(text)
    weekly = parse_weekly_shards(text)
    tiers = parse_watch_countries(text)

    d = datetime.date.fromisoformat(date_str)
    iso = d.isoweekday()
    # 档位全列 + 去重保序(weekly_shards 若仍引用某档, 防同档重复进清单)
    tier_today = list(dict.fromkeys(list(ALWAYS_TIERS) + weekly.get(iso, [])))

    picked = []
    seen_sid = set()
    for tier in tier_today:
        for c in tiers.get(tier, []):
            # shard 过滤已随周轮转废弃(每日全扫); 字段保留解析仅为兼容旧 YAML
            for sid in c["source_ids"]:
                if sid in seen_sid:
                    continue  # 同源跨档/跨国重复挂载, 只扫一次(取首个挂载档位)
                src = sources.get(sid) or {}
                if src.get("track") not in (None, "policy"):
                    continue
                seen_sid.add(sid)
                url = src.get("url") or "(无入口URL, 用官方站检索)"
                picked.append((tier, c["cn"], c["en"], sid, url))
    if group is not None:
        if group < 1:
            raise ValueError("group 从 1 起: %r" % (group,))
        idx = group - 1
        picked = picked[idx * GROUP_CHUNK:(idx + 1) * GROUP_CHUNK]
    return picked


def groups_info(date_str):
    """各块源对数: {1: n1, 2: n2, ...}(块数 = ceil(当日源对数/GROUP_CHUNK))。"""
    n_all = len(_pick(date_str))
    info = {}
    k = 1
    while k * GROUP_CHUNK < n_all + GROUP_CHUNK:
        # 逐块计数(末块可短)
        chunk = _pick(date_str, k)
        if not chunk:
            break
        info[k] = len(chunk)
        k += 1
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
    if group is None:
        header = "今日=%s(%s) 全部档位: %s" % (
            date_str,
            "周" + "一二三四五六日"[datetime.date.fromisoformat(date_str).isoweekday() - 1],
            "/".join(dict.fromkeys(t for t, *_ in picked)) or "(空)")
    else:
        header = "今日=%s(%s) 第%d块/共%d块, 本块 %d 源对" % (
            date_str,
            "周" + "一二三四五六日"[datetime.date.fromisoformat(date_str).isoweekday() - 1],
            group, len(groups_info(date_str)) or 1, n_sel)
    out = header + "\n" + "\n".join(lines)

    # fail-visible 自检: 全量口径正常一天至少 10 行(必扫档 3 档 * 多国); 单块
    # 口径天然可能少(末块只有 1-2 对), 只对"整块为 0"示警。明显偏少说明
    # sources.yaml 书写格式漂移了, 解析器没吃到 —— 宁可让跑批日志大喊,
    # 也不静默空扫。
    if group is None and n_sel < 10:
        out += "\n[WARN] 仅解析到 %d 个国别-源对, 疑似 sources.yaml 格式漂移, 请人工核查!" % n_sel
    if group is not None and n_sel == 0:
        out += "\n[WARN] 第%d块为空 —— 当日总源对数不足以切出这么多块!" % group
    return out, n_sel


def main():
    ap = argparse.ArgumentParser(description="渲染当日应扫国别/源清单(注入取数 prompt)")
    ap.add_argument("--date", default=biz_today(), help="业务日期(东八区), 默认今天")
    ap.add_argument("--group", type=int, default=None,
                    help="只渲染该块(≥1, 每块 %d 源对; 缺省=全部)" % GROUP_CHUNK)
    ap.add_argument("--groups", action="store_true",
                    help="只打印各块源对数(机器可读): n1=.. n2=..")
    args = ap.parse_args()
    try:
        if args.groups:
            info = groups_info(args.date)
            if not info:
                print("n1=0")
            else:
                print(" ".join("n%d=%d" % (k, v) for k, v in sorted(info.items())))
            return 0
        out, n = render(args.date, args.group)
    except (OSError, ValueError) as exc:
        print("[错误] 渲染失败: %s" % exc)
        return 1
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
