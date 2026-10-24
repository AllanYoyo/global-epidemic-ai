#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""疫见全球 · 摘录器(2026-10-24 P2 引入, 工具输出硬上限配套)

为什么需要它: 取数段判断页面内容时"grep/head/python 摘 ≤2KB"是 prompt 纪律,
模型并不总遵守(10-24 实测治理后单次输出仍 13.4KB —— 把已存档 HTML 的链接
清单倒进上下文, 25 次调用累计 115KB)。本脚本**代码层面**保证回显不超限:

  python3 scripts/snip.py data/raw/2026-10-24/04-xxx.html --grep 新城疫
  python3 scripts/snip.py 存档.txt                    # 默认头 2048 字节

- 默认/命中都硬性截到 --bytes(默认 2048)字节, 尾部注明截断与文件总大小;
- --grep: 取首个命中行及其前后各 --bytes/3 字节窗口(中文正文按 errors=replace
  解码, 保证总回显仍 ≤ bytes + 标注行);
- 无命中回一行「无匹配」, 文件不存在回一行错误(rc 1) —— 任何路径都不会倾倒正文。
"""

import argparse
import os
import sys


def _read(path, cap):
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as exc:
        return None, str(exc)
    return data, None


def main():
    ap = argparse.ArgumentParser(description="从文件摘 ≤N 字节片段(硬性截断)")
    ap.add_argument("file", help="目标文件(通常是已落盘的存档)")
    ap.add_argument("--grep", default=None, help="只取首个命中行前后窗口")
    ap.add_argument("--bytes", type=int, default=2048, help="回显硬上限(字节, 默认 2048)")
    args = ap.parse_args()

    data, err = _read(args.file, args.bytes)
    if err:
        print("[snip][错误] 读取失败: %s" % err)
        return 1
    total = len(data)

    if args.grep:
        low = data.lower()
        needle = args.grep.encode("utf-8").lower()
        i = low.find(needle)
        if i < 0:
            print("[snip] 无匹配(文件 %d 字节)" % total)
            return 0
        w = max(args.bytes // 3, 1)
        start = max(i - w, 0)
        chunk = data[start:start + args.bytes]
    else:
        chunk = data[:args.bytes]

    text = chunk.decode("utf-8", errors="replace")
    print(text)
    if total > len(chunk):
        print("…[snip 已截断: 回显 %d 字节 / 文件共 %d 字节]" % (len(chunk), total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
