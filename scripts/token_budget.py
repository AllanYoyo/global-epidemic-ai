#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""疫见全球 · 当日 token 预算刹车(只读, 2026-10-24 引入)

背景(10-24 生产现场): 一天之内被发起 6 个扫描会话(其中 4 个绕过 run_scan.sh、
无超时看护无幂等检查), 而全部 6 条存档其实早上 09:39 就已落齐 —— 之后 4 轮
约 126 万输入 token + 3200 万 cache 读全部白烧, 当晚 Token Plan 打穿(429),
连微信对话都调不动。取数幂等锁(.fetch-done)挡"重复跑", 本脚本挡"今天已经
烧超了还继续烧": run_scan.sh 在启动取数段之前调用, 统计 Hermes state.db 里
**东八区当日**全部会话的消耗, 超过预算返回退出码 3, 由 run_scan.sh 降级为
"只加工已有存档"(数据不丢, 损失止于当日增量)。

口径:
- 折合消耗 = input_tokens + output_tokens + cache_read_tokens × 权重(默认 0.1)。
  cache 读各家中授权重不一, 0.1 是常见定价(缓存读约为输入价的 1/10);
  可用 --cache-weight 或环境变量 SCAN_CACHE_WEIGHT 覆盖。
- 统计"东八区当日 00:00 起"的**所有**会话, 不区分扫描/交互 —— 打穿 Plan 的是总量。
- fail-open: state.db 不存在/读不了 → 打 WARN、退出码 0(测不到就不拦跑批,
  与 scan_targets 渲染失败退回旧指令同一取向; 真超支由 429 与产物校验兜底)。

用法:
  python3 scripts/token_budget.py --date 2026-10-24              # 查当日用量
  python3 scripts/token_budget.py --date 2026-10-24 --budget 4000000
  python3 scripts/token_budget.py --date 2026-10-24 --db /path/to/state.db
退出码: 0=未超预算/无法统计, 3=已达预算上限, 2=参数错误。
"""

import argparse
import datetime
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from normalize import biz_today  # 同级导入, 业务时区口径与 channel_health 一致

BIZ_TZ = datetime.timezone(datetime.timedelta(hours=8))
DEFAULT_BUDGET = 4000000        # 折合 token/日; 基线健康日 ~70 万, 留重试余量
DEFAULT_CACHE_WEIGHT = 0.1


def _default_db():
    env = os.environ.get("HERMES_STATE_DB")
    if env:
        return env
    return os.path.join(os.path.expanduser("~"), ".hermes", "state.db")


def day_window(date_str):
    """东八区某日的 [起点epoch, 终点epoch)。"""
    d = datetime.date.fromisoformat(date_str)
    start = datetime.datetime(d.year, d.month, d.day, tzinfo=BIZ_TZ).timestamp()
    end = start + 86400
    return start, end


def summarize(db_path, start, end):
    """返回 (会话数, 输入, 输出, cache读, top3[(标题, 输入, 输出, cache读)])。

    只读查询; 表缺失/被锁等任何异常向上抛, 由调用方 fail-open。
    """
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(input_tokens),0), "
            "COALESCE(SUM(output_tokens),0), COALESCE(SUM(cache_read_tokens),0) "
            "FROM sessions WHERE started_at >= ? AND started_at < ?",
            (start, end)).fetchone()
        n, in_t, out_t, cr = row
        top = con.execute(
            "SELECT COALESCE(title,'(无标题)'), COALESCE(input_tokens,0), "
            "COALESCE(output_tokens,0), COALESCE(cache_read_tokens,0) "
            "FROM sessions WHERE started_at >= ? AND started_at < ? "
            "ORDER BY COALESCE(input_tokens,0)+COALESCE(output_tokens,0) "
            "      +COALESCE(cache_read_tokens,0) DESC LIMIT 3",
            (start, end)).fetchall()
        return n, in_t, out_t, cr, top
    finally:
        con.close()


def main():
    ap = argparse.ArgumentParser(description="东八区当日 Hermes token 用量/预算检查(只读)")
    ap.add_argument("--date", default=biz_today(), help="业务日期 YYYY-MM-DD(默认东八区今天)")
    ap.add_argument("--budget", type=float,
                    default=float(os.environ.get("SCAN_TOKEN_DAILY_BUDGET") or DEFAULT_BUDGET),
                    help="折合消耗预算(默认 %d, 环境变量 SCAN_TOKEN_DAILY_BUDGET 覆盖)" % DEFAULT_BUDGET)
    ap.add_argument("--cache-weight", type=float,
                    default=float(os.environ.get("SCAN_CACHE_WEIGHT") or DEFAULT_CACHE_WEIGHT),
                    help="cache 读计费权重(默认 0.1)")
    ap.add_argument("--db", default=_default_db(), help="Hermes state.db 路径(默认 ~/.hermes/state.db)")
    args = ap.parse_args()

    try:
        start, end = day_window(args.date)
    except ValueError:
        print("[token_budget] 日期格式应为 YYYY-MM-DD: %s" % args.date)
        return 2

    if not os.path.isfile(args.db):
        # fail-open: 测不到就不拦(本机开发机没有 state.db 是常态)
        print("[token_budget][WARN] 找不到 state.db(%s), 无法统计, 放行(不拦跑批)" % args.db)
        return 0
    try:
        n, in_t, out_t, cr, top = summarize(args.db, start, end)
    except sqlite3.Error as exc:
        print("[token_budget][WARN] state.db 读取失败(%s), 放行(不拦跑批)" % exc)
        return 0

    units = in_t + out_t + cr * args.cache_weight
    print("[token_budget] %s(东八区) Hermes 会话 %d 个: 输入 %d / 输出 %d / cache读 %d(权重%.2f)" % (
        args.date, n, in_t, out_t, cr, args.cache_weight))
    for t, i2, o2, c2 in top:
        print("[token_budget]   · %s(输入 %d / 输出 %d / cache读 %d)" % (
            (t or "(无标题)")[:40].replace("|", " "), i2, o2, c2))
    verdict = "已达预算上限" if units >= args.budget else "预算内"
    print("[token_budget] 折合消耗 %d / 预算 %d —— %s" % (round(units), round(args.budget), verdict))
    return 3 if units >= args.budget else 0


if __name__ == "__main__":
    sys.exit(main())
