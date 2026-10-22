#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一条网络情报存档到 data/raw/: 抓取 URL 或登记本地文件, 保留来源元信息。

这是"可溯源链"的第一环: 侦察到的每个页面都应先在这里留下原始副本 + meta。

用法:
  python collect.py --url https://example.com/notice --title "WOAH ASF 通报"
  python collect.py --file 已保存的正文.txt --url https://... --title 标题

输出: data/raw/<日期>/<序号>-<slug>.<ext> + 同名 .meta.json, 并打印保存路径。
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

# 单轮存档量上限(2026-10-22 加固): 10-22 实测 Agent 逐条下载 WTO 通报 PDF 核
# 发布日期, 存了 23+ 个文件、跑了 40 分钟没完(正常一轮只存 2 个)。这里做**代码
# 强制**的上限, 不依赖 Agent 自律: 触顶即拒绝, 并提示"窗口外的条目不该存档、
# 留待下一轮"。可用 --max-files-per-day 调整, 0 = 不限。
DEFAULT_MAX_FILES_PER_DAY = 12


def biz_now():
    """业务时间(东八区) ISO8601。"""
    return (datetime.datetime.utcnow()
            + datetime.timedelta(hours=BIZ_TZ_OFFSET_HOURS)).isoformat(timespec="seconds")


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
    ap.add_argument("--max-files-per-day", type=int, default=DEFAULT_MAX_FILES_PER_DAY,
                    help="当日存档文件数上限(触顶即拒绝新增), 默认 12; 0 = 不限")
    args = ap.parse_args()

    if not args.url and not args.file:
        ap.error("--url 与 --file 至少提供一个")
    if args.file and not os.path.isfile(args.file):
        ap.error("文件不存在: %s" % args.file)

    # 当日存档量上限(2026-10-22): 触顶即拒绝 —— 防止"逐条下载核日期"式穷举
    # 把一轮扫描拖到几十分钟(10-22 实测 23+ 个文件 / 40 分钟没完)。
    # 放在**抓取之前**判断: 否则先把内容下载下来再拒绝, 白费带宽。
    out_dir = os.path.join(DATA_DIR, "raw", args.date)
    os.makedirs(out_dir, exist_ok=True)
    existing = len(os.listdir(out_dir))
    if args.max_files_per_day and existing >= args.max_files_per_day:
        print("[拒绝] %s 已有 %d 个存档文件(上限 %d), 本轮不再新增。"
              % (args.date, existing, args.max_files_per_day))
        print("       10-22 实测: 逐条下载 WTO 通报 PDF 核发布日期, 存了 23+ 个文件、")
        print("       跑了 40 分钟没完(正常一轮只存 2 个)。**窗口外的条目不该存档**。")
        print("       把本轮剩下的通道标进 search-log.md, 留待下一轮。")
        return 1

    body = open(args.file, encoding="utf-8", errors="replace").read() if args.file else fetch(args.url)

    # 当日存档量上限(2026-10-22): 触顶即拒绝 —— 防止"逐条下载核日期"式穷举
    # 把一轮扫描拖到几十分钟(10-22 实测 23+ 个文件 / 40 分钟没完)。
    existing = len(os.listdir(out_dir))
    if args.max_files_per_day and existing >= args.max_files_per_day:
        print("[拒绝] %s 已有 %d 个存档文件(上限 %d), 本轮不再新增。"
              % (args.date, existing, args.max_files_per_day))
        print("       10-22 实测: 逐条下载 WTO 通报 PDF 核发布日期, 存了 23+ 个文件、")
        print("       跑了 40 分钟没完(正常一轮只存 2 个)。**窗口外的条目不该存档**。")
        print("       把本轮剩下的通道标进 search-log.md, 留待下一轮。")
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
