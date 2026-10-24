#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""疫见全球 · JSONL 单行追加器(2026-10-24 P2 引入, 工具输出硬上限配套)

为什么需要它: 取数段「边存边写」曾用 execute_code 现场拼 python —— 每追加一条
就是一段新代码进上下文(模型输出 token), 还时常顺手把整条记录打印回来「验证」
(10-24 实测单次 execute_code 输出 13.4KB, 25 次调用累计 115KB, prompt 的
8KB 红线是纸面约束)。本脚本把追加变成**一行短命令**:

  python3 scripts/jsonl_append.py data/raw/2026-10-24/candidates.jsonl \
      title='欧盟修订苯醚甲环唑限量' url='https://eur-lex.example/x' country_en=EU

- 键值对解析: 等号首个为界; 值为空 → 存 null(数据合同「原文没有的信息填 null」);
- 点号键表达嵌套: source.url=... / source.quote=... 会落成 {"source": {"url": ...}};
- products 之类的列表: 逗号分隔自动拆 list(prods=a,b → ["a","b"]);
- **只回执一行**(ok <文件> +1 (total N)), 任何情况下不回显记录内容 ——
  模型想违反输出纪律也违反不了;
- 标题/URL 缺一即拒绝追加(rc 1), 防止写进残行 —— 标题认 title 或 title_cn
  (抽取记录用 title_cn), URL 认 url 或 source.url(抽取记录用 source.url)。
"""

import argparse
import json
import os
import sys


def _set_nested(record, key, value):
    """source.url=v 落成嵌套 dict; 普通键直接赋值。"""
    parts = key.split(".")
    node = record
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = value


def parse_kv(args):
    record = {}
    for a in args:
        if "=" not in a:
            return None, a
        k, v = a.split("=", 1)
        if not k:
            return None, a
        if v == "":
            v = None
        _set_nested(record, k, v)
    # products 一类列表字段: 逗号拆分(仅在值非空且是字符串时)
    for listkey in ("products", "topics"):
        v = record.get(listkey)
        if isinstance(v, str) and v:
            record[listkey] = [s.strip() for s in v.split(",") if s.strip()]
    return record, None


def main():
    ap = argparse.ArgumentParser(description="向 .jsonl 追加一行记录(只回执一行)")
    ap.add_argument("file", help="目标 .jsonl 文件(不存在则创建)")
    ap.add_argument("kv", nargs="+", help="key=value 键值对(点号嵌套, 空值存 null)")
    args = ap.parse_args()

    record, bad = parse_kv(args.kv)
    if record is None:
        print("[jsonl_append][错误] 参数不是 key=value 形式: %s" % bad)
        return 1
    url = record.get("url") or (record.get("source") or {}).get("url")
    title = record.get("title") or record.get("title_cn")
    if not title or not url:
        print("[jsonl_append][错误] 缺标题(title/title_cn)或 URL(url/source.url), 拒绝追加(残行不入库)")
        return 1

    try:
        os.makedirs(os.path.dirname(os.path.abspath(args.file)), exist_ok=True)
        with open(args.file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        print("[jsonl_append][错误] 写入失败: %s" % exc)
        return 1
    with open(args.file, encoding="utf-8") as f:
        n = sum(1 for line in f if line.strip())
    print("ok %s +1 (total %d)" % (os.path.basename(args.file), n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
