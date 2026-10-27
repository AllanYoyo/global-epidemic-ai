#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一条网络情报存档到 data/raw/: 抓取 URL 或登记本地文件, 保留来源元信息。

这是"可溯源链"的第一环: 侦察到的每个页面都应先在这里留下原始副本 + meta。

用法:
  python collect.py --url https://example.com/notice --title "WOAH ASF 通报"
  python collect.py --file 已保存的正文.txt --url https://... --title 标题

输出: data/raw/<日期>/<序号>-<slug>.<ext> + 同名 .meta.json, 并打印保存路径。

当日存档量上限: --max-archives-per-day(默认 12 条), 按**存档条数**计
(数 *.meta.json; 交付文件不占配额), 触顶即拒绝新增。旧参数名 --max-files-per-day
保留为别名。
"""
import argparse
import datetime
import json
import os
import re
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("EPIDEMIC_DATA_DIR") or os.path.join(REPO, "data")
UA = os.environ.get("HTTP_USER_AGENT") or \
    "Mozilla/5.0 (compatible; GlobalEpidemicRadar/0.1; +https://github.com/global-epidemic-ai)"
# 业务时区固定东八区: 生产 VPS 系统时区是 Etc/UTC, 直接用 date.today() 会让
# 北京时间早间归档落到前一天目录。与 normalize.biz_today 同口径。
BIZ_TZ_OFFSET_HOURS = 8

# 单轮存档量上限(2026-10-22 加固; 2026-10-27 口径修正): 10-22 实测 Agent 逐条下载
# WTO 通报 PDF 核发布日期, 存了 23+ 个文件、跑了 40 分钟没完(正常一轮只存 2 个)。
# 这里做**代码强制**的上限, 不依赖 Agent 自律: 触顶即拒绝, 并提示"窗口外的条目不该
# 存档、留待下一轮"。可用 --max-archives-per-day 调整, 0 = 不限。
#
# 2026-10-27 口径修正(A1): 原实现用 len(os.listdir(out_dir)) 数**目录文件总数**,
# 而每条存档产出**两个**文件(正文 + .meta.json), 再叠加取数段的交付文件
# (candidates.jsonl / search-log.md / extracted-policy.jsonl / policy-findings.md /
# .fetch-done), 于是"上限 12"实际只存得下 **6 条**存档 —— 隔离实测(上限 12):
# 第 6 次成功(目录 12 个文件 / 6 条存档), 第 7 次被拒。
# 而全链路其余环节(fetch_pages 的 already 计数、run_scan.sh 的 RAW_N 终检)都按
# *.meta.json 计**存档条数**, 两套口径不一致 → 系统各处以为能存 12 条, 真实每日
# 只有 6 条, 47 个源的每日覆盖被悄悄压掉一半。
# 现在统一按存档条数计: 每条存档恰写一个 .meta.json, 数它即得条数; 交付文件不占配额。
DEFAULT_MAX_ARCHIVES_PER_DAY = 12


def biz_now():
    """业务时间(东八区) ISO8601。"""
    return (datetime.datetime.utcnow()
            + datetime.timedelta(hours=BIZ_TZ_OFFSET_HOURS)).isoformat(timespec="seconds")


def count_archives(out_dir):
    """当日已有存档**条数**(数 *.meta.json)。

    每条存档恰写一个 .meta.json(正文 + .meta.json 成对落盘), 所以 .meta.json
    的个数 == 存档条数。交付文件(candidates.jsonl / search-log.md /
    extracted-policy.jsonl / policy-findings.md / .fetch-done)**不计入** ——
    它们是过程产物, 不占存档配额。
    """
    try:
        return sum(1 for f in os.listdir(out_dir) if f.endswith(".meta.json"))
    except OSError:
        return 0


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
        ctype = resp.headers.get("Content-Type", "")
    m = re.search(r"charset=([\w-]+)", ctype)
    enc = m.group(1) if m else "utf-8"
    try:
        return body.decode(enc, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def main():
    ap = argparse.ArgumentParser(description="情报存档(原始副本 + 来源元信息)")
    ap.add_argument("--url", help="来源 URL(抓取正文存档)")
    ap.add_argument("--file", help="本地正文文件(如 agent 已抓取的文本), 登记入库")
    ap.add_argument("--title", help="标题, 用于文件名与 meta")
    ap.add_argument("--date", default=biz_now()[:10], help="归档日期目录, 默认今天(东八区)")
    ap.add_argument("--kind", choices=["html", "txt"], help="保存扩展名(默认: 有 --file 为 txt, 否则 html)")
    ap.add_argument("--max-archives-per-day", type=int, default=None,
                    help="当日存档**条数**上限(触顶即拒绝新增), 默认 12; 0 = 不限。"
                         "按 *.meta.json 计, 交付文件(candidates.jsonl/search-log.md 等)不占配额")
    ap.add_argument("--max-files-per-day", type=int, default=None,
                    help="[旧名, 已更名] 当日存档上限; 语义同 --max-archives-per-day"
                         "(2026-10-27 起按存档条数计, 交付文件不占配额)")
    args = ap.parse_args()

    if not args.url and not args.file:
        ap.error("--url 与 --file 至少提供一个")
    if args.file and not os.path.isfile(args.file):
        ap.error("文件不存在: %s" % args.file)

    # ---- 当日存档上限参数: 新名优先, 旧名兼容; 两个都给且不一致 → 报错退出 ----
    # 上限口径的歧义会让"今天到底能存几条"变成猜, 与本仓库 fail-closed 取向相反。
    cap = args.max_archives_per_day
    if args.max_files_per_day is not None:
        if cap is not None and cap != args.max_files_per_day:
            print("[错误] --max-archives-per-day(%s) 与 --max-files-per-day(%s) 不一致, 已中止。"
                  % (cap, args.max_files_per_day))
            print("       --max-files-per-day 是旧名(2026-10-27 起按存档条数计), 请只用其中一个。")
            return 2
        cap = args.max_files_per_day
    if cap is None:
        cap = DEFAULT_MAX_ARCHIVES_PER_DAY

    # 当日存档量上限(2026-10-22): 触顶即拒绝 —— 防止"逐条下载核日期"式穷举
    # 把一轮扫描拖到几十分钟(10-22 实测 23+ 个文件 / 40 分钟没完)。
    # 放在**抓取之前**判断: 否则先把内容下载下来再拒绝, 白费带宽。
    # 2026-10-27: 按**存档条数**计(见 count_archives), 不再被正文/meta 成对落盘
    # 与交付文件挤占配额。
    out_dir = os.path.join(DATA_DIR, "raw", args.date)
    os.makedirs(out_dir, exist_ok=True)
    n_arch = count_archives(out_dir)
    if cap and n_arch >= cap:
        print("[拒绝] %s 已有 %d 条存档(上限 %d), 本轮不再新增。"
              % (args.date, n_arch, cap))
        print("       10-22 实测: 逐条下载 WTO 通报 PDF 核发布日期, 存了 23+ 个文件、")
        print("       跑了 40 分钟没完(正常一轮只存 2 个)。**窗口外的条目不该存档**。")
        print("       把本轮剩下的通道标进 search-log.md, 留待下一轮。")
        print("       (上限按**存档条数**计: 每条存档 = 1 个正文 + 1 个 .meta.json;")
        print("        candidates.jsonl / search-log.md 等交付文件不占配额)")
        return 1

    body = open(args.file, encoding="utf-8", errors="replace").read() if args.file else fetch(args.url)

    # 抓取之后再查一次: 抓取期间可能有并发 collect 落盘(两条存档同时越过上限线)。
    n_arch = count_archives(out_dir)
    if cap and n_arch >= cap:
        print("[拒绝] %s 已有 %d 条存档(上限 %d), 本轮不再新增。"
              % (args.date, n_arch, cap))
        print("       窗口外的条目不该存档; 把本轮剩下的通道标进 search-log.md, 留待下一轮。")
        return 1
    slug = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", args.title or args.url or "record").strip("-")[:40] or "record"
    seq = 1 + sum(1 for f in os.listdir(out_dir) if f.endswith(".meta.json"))
    ext = args.kind or ("txt" if args.file else "html")
    base = "%02d-%s" % (seq, slug)

    with open(os.path.join(out_dir, base + "." + ext), "w", encoding="utf-8") as f:
        f.write(body)
    meta = {
        "title": args.title,
        "url": args.url,
        "file": base + "." + ext,
        "local_source_file": args.file,
        "fetched_at": biz_now(),
        "tool": "collect.py",
    }
    with open(os.path.join(out_dir, base + ".meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print("[OK] %s" % os.path.join(out_dir, base + "." + ext))
    print("     meta: %s.meta.json" % base)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
