#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""疫见全球 · 通道体检(取数段"死通道跳过名单"生成器, 2026-10-23 引入)

背景: 10-22/10-23 两日实测, 取数段每天用 30-50 轮工具调用重新"发现"同一批
不可用通道(403/连接失败/占位页), 而每轮工具调用都把全部历史重新发给模型 ——
这是检索烧 token 的主要浪费之一(10-23 实测取数段 90 轮/12.9M 输入 token)。
本脚本从近几日 data/raw/<日期>/search-log.md **确定性**提取不可用通道,
生成跳过名单文本, 由 run_scan.sh 注入取数 prompt; 幂等、只读、不联网、不编造。

口径:
- 死亡 = 通道状态表"结果"列含"不可用"(403/连接失败/占位页等均记为该口径)。
- 最近两个"有日志的日子"都死 → 跳过名单(本轮直接跳过);
- 仅最近一日死 → 复检名单(至多一次, 5 秒超时);
- 只有一天有日志 → 全部进复检名单(单日证据不足以下"死"结论)。
- 通道名跨日写法常有出入(如"美国 APHIS"/"美国 APHIS 官网"), 匹配前做
  去空白/去括号/小写归一; 展示名用最近一日的写法。

用法(由 run_scan.sh 调用, 也可手工排查):
  python3 scripts/channel_health.py --date 2026-10-23
  python3 scripts/channel_health.py --date 2026-10-23 --days 3
输出: 有名单时打印一段可直接拼进 prompt 的中文文本; 无任何死通道记录时
不输出(退出码恒为 0, 失败只打 stderr 警告, 不阻塞跑批)。
"""

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from normalize import biz_today  # 同级导入, 与 risk.py 一致

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("EPIDEMIC_DATA_DIR") or os.path.join(REPO, "data")


def _norm_key(name):
    """通道名归一: 去空白/括号/标点/分隔符并小写, 供跨日匹配。"""
    return re.sub(r"[\s()（）\[\]【】\-—_/\\.·、,，:：]", "", name).lower()


def parse_day(path):
    """解析一天 search-log.md, 返回 {归一键: (展示名, 原因)}。

    兼容表格式(主流): | 通道 | **不可用** | 原因 |; 通道格可能斜杠并列多站
    ("加拿大 CFIA / 印度 DAHD"), 按空格斜杠拆开。非表格式行忽略。
    """
    dead = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return dead
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cols = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cols) < 2 or "不可用" not in cols[1]:
            continue
        reason = (cols[2].strip()[:24] if len(cols) > 2 and cols[2].strip() else "不可用")
        for raw in re.split(r"\s*/\s*", cols[0]):
            name = raw.strip()
            if name and not set(name) <= set("-— "):
                dead.setdefault(_norm_key(name), (name, reason))
    return dead


def _join(items, limit):
    """拼名单, 超长截断并注明总数。"""
    text = "、".join("%s(%s)" % (n, r) for n, r in items)
    if len(text) > limit:
        text = text[:limit] + "…(共 %d 个, 已截断)" % len(items)
    return text


def main():
    ap = argparse.ArgumentParser(description="从近几日 search-log.md 生成取数段死通道跳过名单")
    ap.add_argument("--date", default=biz_today(), help="业务日期 YYYY-MM-DD(默认东八区今天)")
    ap.add_argument("--days", type=int, default=3, help="回看天数(默认 3)")
    ap.add_argument("--max-chars", type=int, default=1200, help="单个名单最大字符数")
    args = ap.parse_args()

    import datetime as _dt
    try:
        base = _dt.date.fromisoformat(args.date)
    except ValueError:
        print("[channel_health] 日期格式应为 YYYY-MM-DD: %s" % args.date, file=sys.stderr)
        return 0

    # 依次找最近的有日志日子(最多回看 args.days 天)
    avail = []
    for i in range(1, args.days + 1):
        d = base - _dt.timedelta(days=i)
        path = os.path.join(DATA_DIR, "raw", d.isoformat(), "search-log.md")
        if os.path.isfile(path):
            dead = parse_day(path)
            avail.append(dead)
            if len(avail) >= 2:
                break
    if not avail:
        return 0

    recent = avail[0]
    lines = ["【通道体检(确定性生成, 依据近几日 search-log.md)】"]
    if len(avail) >= 2:
        older = avail[1]
        both = [(recent[k][0], recent[k][1]) for k in recent if k in older]
        once = [(recent[k][0], recent[k][1]) for k in recent if k not in older]
        if both:
            lines.append("- 以下通道**连续两日不可用**, 本轮直接跳过、不再探测: %s" % _join(both, args.max_chars))
        if once:
            lines.append("- 以下通道昨日不可用, 至多**一次快速复检**(单通道超时 5 秒, 仍失败即放弃): %s" % _join(once, args.max_chars))
    else:
        if recent:
            lines.append("- 以下通道近日不可用(仅一日证据), 至多**一次快速复检**(单通道超时 5 秒): %s"
                         % _join(list(recent.values()), args.max_chars))
    if len(lines) > 1:
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
