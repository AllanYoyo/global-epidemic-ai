#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据清理: 删除 N 天前的取数存档/事件文件/日报文件(2026-11-21 用户拍板)。

为什么做: 取数存档(data/raw/<日期>/)是磁盘大头 —— 每天每源的正文存档 + meta +
交付文件无限累积, 日报文件(md/docx/xlsx)与幂等/闸门标记同理。清理口径由用户拍板:

  · 台账数据库(events 表)**暂不清**(2026-11-21 用户拍板"听你的, 暂时不清"):
    台账是主体资产, 454 条才 1~2MB, 删了没有磁盘收益, 却丢掉面板历史、老政策的
    语义查重演化(WTO 通报天天重发, 靠库里的旧记录做"更丰富者胜"合并更新 ——
    删了之后老政策重发会退化成"旧闻拒收", 记录停止演化)与溯源完整性。
  · database/fetch.db **绝不能动**: urlwatch 条件请求状态(ETag/指纹), 删了
    47 源全量重抓一轮、指纹全部重基线。
  · data/geocache.json / logs/ 不动: 缓存与运行日志不属本轮口径; 日志是单文件
    追加, 按行截断与 cron 写入有竞态, 按需人工处理。

清什么(全部按**目录/文件名里的日期**判定, 删"严格早于截止日"的, 截止日当天保留):
  · data/raw/<日期>/        整目录(正文存档 + meta + search-log + 交付文件 + .fetch-done)
  · data/events/<日期>/     整目录(写透 JSON 快照; 台账以 DB 为权威, 删了不影响台账;
                            merge_duplicates 对它的 JSON 清理是尽力而为, 有 --no-clean-json)
  · data/reports/<日期>-policy-report.{md,docx,xlsx,csv}   日报三件套 + 降级 CSV
  · data/reports/.pushed|.review-pending|.push-scope/<日期>.*   幂等/闸门标记
    (只对当天有意义, 过期即死纸; M38/M40/M41 的闸门都只读"当天"的标记)

安全纪律:
  · 默认 dry-run 只预览; --apply 才真删。apply 先把**删除清单**写进
    data/reports/.prune/<时间戳>.log 再动手(删除类操作必须留痕, 半途被杀也
    能从清单知道删到哪)。
  · --days 下限 10: 跨天补抽(EXTRACT_CARRY_DAYS 默认 3)、channel_health(默认
    回看 3 天)、dead_sources(默认回看 7 天)还要回看近期的 search-log.md,
    保留窗口必须盖过最大读者, 否则清理会打断死源分诊与死通道治理。
  · 日期解析不了的条目一律跳过不动(宁可漏删不错删)。
  · 业务日期走 normalize.biz_today()(东八区), 不用宿主机 date.today() ——
    生产 VPS 系统时区是 UTC, 直接取宿主机日期会让截止日整体错一天。

用法:
  python scripts/prune_data.py                # 预览(默认 dry-run, 不删任何东西)
  python scripts/prune_data.py --apply        # 执行清理(默认 30 天)
  python scripts/prune_data.py --days 60      # 窗口覆盖
退出码: 0=预览完成/清理全部成功; 1=参数拒绝或有删除项失败。
"""
import argparse
import datetime
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from normalize import biz_now, biz_today  # 东八区业务日期/时间戳, 与全仓口径一致

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("EPIDEMIC_DATA_DIR") or os.path.join(REPO, "data")

DEFAULT_DAYS = 30
# 保留窗口下限: 必须盖过旧数据的最大读者回看窗口
# (跨天补抽 EXTRACT_CARRY_DAYS=3 / channel_health 默认 3 天 / dead_sources 默认 7 天)
MIN_DAYS = 10
REPORT_SUFFIXES = (".md", ".docx", ".xlsx", ".csv")
MARKER_PARENTS = (".pushed", ".review-pending", ".push-scope")
MARKER_SUFFIXES = (".flag", ".lock", ".partial")

# 展示分组: kind -> 标签
KIND_LABELS = (
    ("raw", "取数存档目录(data/raw/<日期>/)"),
    ("events", "事件快照目录(data/events/<日期>/)"),
    ("reports", "日报文件(data/reports/<日期>-policy-report.*)"),
    ("markers", "幂等/闸门标记(data/reports/.pushed 等)"),
)


def _iso_date(s):
    """严格解析 YYYY-MM-DD; 不是合法日历日期返回 None(不猜)。"""
    if not isinstance(s, str) or len(s) != 10:
        return None
    try:
        return datetime.date.fromisoformat(s)
    except ValueError:
        return None


def _dir_size(path):
    """目录占用的字节数(递归)。"""
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _human(n):
    if n >= 1024 * 1024:
        return "%.1f MB" % (n / 1024.0 / 1024.0)
    if n >= 1024:
        return "%.1f KB" % (n / 1024.0)
    return "%d B" % n


def collect_targets(cutoff):
    """收集"严格早于 cutoff"的待清理对象。

    返回 (targets, skipped):
      targets = [(kind, path, size, date), ...]
      skipped = [(相对路径, 原因), ...]  —— 日期解析不了的条目, 宁可漏删不错删
    """
    targets, skipped = [], []

    # ---- 日期目录: data/raw/<日期>/ 与 data/events/<日期>/ ----
    for sub in ("raw", "events"):
        base = os.path.join(DATA_DIR, sub)
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            p = os.path.join(base, name)
            d = _iso_date(name)
            if d is None:
                skipped.append((os.path.join(sub, name), "目录名不是 YYYY-MM-DD"))
                continue
            if d >= cutoff:
                continue
            if os.path.isdir(p):
                targets.append((sub, p, _dir_size(p), d))
            else:
                # raw/events 下的散文件(非目录)不在清理口径内 —— 口径只认日期目录
                skipped.append((os.path.join(sub, name), "同名散文件不在清理口径(只清日期目录)"))

    # ---- 日报文件与标记: data/reports/ ----
    rep = os.path.join(DATA_DIR, "reports")
    if os.path.isdir(rep):
        for name in sorted(os.listdir(rep)):
            p = os.path.join(rep, name)
            if not os.path.isfile(p):
                continue  # .pushed 等子目录 / .prune 留痕目录在下面单独处理
            d = _iso_date(name[:10])
            if d is not None and name[10:].startswith("-policy-report.") \
                    and name.endswith(REPORT_SUFFIXES) and d < cutoff:
                targets.append(("reports", p, os.path.getsize(p), d))
        for parent in MARKER_PARENTS:
            mbase = os.path.join(rep, parent)
            if not os.path.isdir(mbase):
                continue
            for name in sorted(os.listdir(mbase)):
                p = os.path.join(mbase, name)
                if not os.path.isfile(p):
                    continue
                d = _iso_date(name[:10])
                if d is not None and name[10:] in MARKER_SUFFIXES and d < cutoff:
                    targets.append(("markers", p, os.path.getsize(p), d))
    return targets, skipped


def _append_log(path, line):
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    ap = argparse.ArgumentParser(description="数据清理: 删除 N 天前的存档/事件文件/日报(台账 DB 不动)")
    ap.add_argument("--date", default=biz_today(), help="业务日期基准 YYYY-MM-DD(默认东八区今天)")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS,
                    help="保留窗口(天): 严格早于「基准日期 - N 天」的对象被清理; 默认 %d, 下限 %d"
                         % (DEFAULT_DAYS, MIN_DAYS))
    ap.add_argument("--apply", action="store_true",
                    help="执行清理(默认 dry-run 只预览; 删除清单写进 data/reports/.prune/ 留痕)")
    args = ap.parse_args()

    base_date = _iso_date(args.date)
    if base_date is None:
        print("[错误] --date 不是合法日期: %r" % args.date)
        return 1
    if args.days < MIN_DAYS:
        print("[错误] --days=%d 低于下限 %d: 保留窗口必须盖过旧数据的最大读者"
              "(跨天补抽回看 %s 天、channel_health 回看 3 天、dead_sources 回看 7 天), "
              "否则清理会打断死源分诊与死通道治理"
              % (args.days, MIN_DAYS,
                 os.environ.get("EXTRACT_CARRY_DAYS") or "3"))
        return 1
    cutoff = base_date - datetime.timedelta(days=args.days)
    targets, skipped = collect_targets(cutoff)

    if not targets:
        print("[OK] 截止 %s(保留最近 %d 天)之前没有可清理对象, 什么都不用做" % (cutoff, args.days))
        return 0

    # ---- 分组汇总 ----
    by_kind = {}
    for kind, path, size, _d in targets:
        by_kind.setdefault(kind, [0, 0])
        by_kind[kind][0] += 1
        by_kind[kind][1] += size
    total = sum(s for _k, _p, s, _d in targets)

    print("[预览] 截止 %s(保留最近 %d 天, 基准日期 %s), 待清理 %d 项, 合计约 %s:"
          % (cutoff, args.days, base_date, len(targets), _human(total)))
    for kind, path, size, _d in targets:
        print("  - [%s] %s (%s)" % (kind, path, _human(size)))
    if skipped:
        print("[提示] %d 个条目日期解析不了, 一律不动(宁可漏删不错删):" % len(skipped))
        for rel, why in skipped:
            print("  - %s (%s)" % (rel, why))
    print("[不碰] database/(台账 DB 与 fetch.db 状态)、data/geocache.json、logs/、"
          "截止日当天及窗口内的所有文件")

    if not args.apply:
        print("[dry-run] 以上未执行。确认无误后: python scripts/prune_data.py --apply")
        return 0

    # ---- 执行: 先落盘删除清单再动手(半途被杀也能从清单知道删到哪) ----
    prune_dir = os.path.join(DATA_DIR, "reports", ".prune")
    log_path = os.path.join(prune_dir,
                            "prune-%s.log" % biz_now().replace(":", "").replace("-", ""))
    try:
        os.makedirs(prune_dir, exist_ok=True)
        _append_log(log_path, "# 数据清理 %s · 基准 %s · 截止 %s(保留 %d 天) · 共 %d 项"
                    % (biz_now(), base_date, cutoff, args.days, len(targets)))
        for kind, path, size, _d in targets:
            _append_log(log_path, "计划 [%s] %s (%s)" % (kind, path, _human(size)))
    except OSError as exc:
        # 删除类操作不留痕不执行: 留痕写不出来就整体中止, 宁可这轮不清理
        print("[失败] 无法写清理留痕(%s): %s —— 已中止, 未删除任何东西" % (log_path, exc))
        return 1

    n_ok = n_err = 0
    for kind, path, size, _d in targets:
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            n_ok += 1
            _append_log(log_path, "已删 [%s] %s (%s)" % (kind, path, _human(size)))
        except OSError as exc:
            n_err += 1
            _append_log(log_path, "失败 [%s] %s: %s" % (kind, path, exc))
            print("[失败] 删除 %s: %s" % (path, exc))

    _append_log(log_path, "# 完成 %s: 成功 %d / 失败 %d, 释放约 %s"
                % (biz_now(), n_ok, n_err, _human(total)))
    if n_err:
        print("[失败] 清理完成但有 %d 项删除失败(成功 %d 项), 留痕: %s" % (n_err, n_ok, log_path))
        return 1
    print("[OK] 清理完成: 成功 %d 项, 释放约 %s; 删除清单留痕: %s" % (n_ok, _human(total), log_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
