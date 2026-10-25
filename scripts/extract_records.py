#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""疫见全球 · 单轮微调用抽取器(2026-10-25 P4 引入)—— 每份存档一次 LLM 调用

为什么需要它: Agent 会话式抽取对"轮次上限/Token 耗尽/阶段超时"任何一种死法
都是整轮全损(10-25 三连败实证)。本脚本把抽取切成**每份存档一次单轮调用**:

  对每个已存档但未抽取的 meta.json → snip 摘录(≤4KB) + 字段模板拼 prompt
  → 经 HERMES_RUN_CMD 模板发起 hermes -z(单轮, 无工具循环)
  → 模型输出 key=value 行 → 本地解析校验 → 追加 extracted-policy.jsonl

- 单份失败(429/超时/输出不合规格)只废那一份: 重试 1 次, 再失败记 search-log
  跳过; 剩余文档继续 —— 429 打不断管线, 只降级当轮覆盖;
- 输出格式用 key=value 行(不用 JSON): prompt 经 shell 双引号拼进 hermes -z,
  prompt 与输出都不能含 ASCII 双引号/反引号/$(仓库既有红线), key=value 天然规避;
- 影响研判字段并入同一次调用(文档上下文内判定), risk.py 写回由本脚本在
  normalize 之后按当日新入库记录统一执行(确定性)。

用法(由 run_scan.sh 调用):
  python3 scripts/extract_records.py --date 2026-10-25
  python3 scripts/extract_records.py --date 2026-10-25 --stage extract-only
"""

import argparse
import datetime
import glob
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from normalize import biz_today, DATA_DIR

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CALL_TIMEOUT = 150          # 单次微调用上限(秒)
SNIP_BYTES = 4096           # 每份文档进 prompt 的摘录上限

# 字段模板(与 Agent 版 prompt 同一套键, 空值模型会留空 → 存 null)
FIELD_TEMPLATE = """record_type=policy
title_cn=<中文标题>
title_en=<英文标题>
country_cn=<国名中文>
country_en=<国名英文>
policy_domain=<animal|plant|both|trade|measures>
action_type=<收紧|放松|调整|恢复>
event_date=<YYYY-MM-DD>
effective_date=<YYYY-MM-DD 或留空>
policy_key=<文号/决议号, 如 G/SPS/N/PER/1128 或 第247号令; 原文没有则用标题核心词(event_id 关联对象必填)>
products=<英文逗号分隔商品, 原文没有则留空>
target_countries=<措施针对的输出入国, 英文逗号分隔, 没有则留空>
summary_cn=<中文摘要一句话>
source_tier=<1|3>
source_name=<源名>
source_url=<存档URL>
source_publish_date=<YYYY-MM-DD>
source_quote=<≤120字逐字摘自下方原文, 超出宁可截断>
impact_type=<约束|机会|中性>
china_relevance=<直接涉及中国|间接影响|暂无明显关联>
dim_trade=<0-5 整数>
dim_biosecurity=<0-5 整数>
dim_response=<0-5 整数>
dim_alignment=<0-5 整数>
impact_rationale=<研判理由一句话>"""

PROMPT_TPL = """你是动植物检疫政策抽取器。从下面这份政策原文摘录里抽取一条政策记录。
只输出 key=value 行(每行一个字段, 值内不要换行), 不输出任何解释、标题或代码块。
字段与取值要求如下(%s 为发布日期, 超过 7 天的旧文件仍照实抽取, 入库端会拦):
%s
【存档元信息】
url=%s
title=%s
【原文摘录(逐字, 已截断)】
%s
现在只输出 key=value 行。"""


def _clean(s):
    return str(s or "").replace('"', "」").replace("`", "").replace("$", "")


def load_archives(date_str):
    """[(meta_path, meta_dict, 正文文本), ...] —— 正文只收 .txt/.html(可解码)。"""
    raw_dir = os.path.join(DATA_DIR, "raw", date_str)
    out = []
    for mp in sorted(glob.glob(os.path.join(raw_dir, "*.meta.json"))):
        try:
            with open(mp, encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        url = meta.get("url") or ""
        body = ""
        base = mp[:-len(".meta.json")]
        for ext in (".txt", ".html"):
            p = base + ext
            if os.path.isfile(p):
                try:
                    with open(p, "rb") as f:
                        raw = f.read(SNIP_BYTES * 2)
                    text = raw.decode("utf-8", errors="replace")
                    # html: 去标签留正文, 压空白
                    if ext == ".html":
                        text = re.sub(r"<script.*?</script>|<style.*?</style>", " ",
                                      text, flags=re.S | re.I)
                        text = re.sub(r"<[^>]+>", " ", text)
                    body = re.sub(r"\s+", " ", text)[:SNIP_BYTES * 2]
                except OSError:
                    pass
                break
        out.append((mp, meta, url, body))
    return out


def done_urls(date_str):
    """已抽取过的 url 集合(extracted-policy.jsonl 逐行)。"""
    p = os.path.join(DATA_DIR, "raw", date_str, "extracted-policy.jsonl")
    urls = set()
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    urls.add(json.loads(line).get("source", {}).get("url")
                             or json.loads(line).get("url"))
                except ValueError:
                    pass
    return urls


def build_prompt(meta, url, body, today):
    return PROMPT_TPL % (today, FIELD_TEMPLATE, _clean(url),
                         _clean(meta.get("title") or "")[:120],
                         _clean(body) if body else "(正文不可读, 仅凭标题与URL抽取, quote 填标题)")


def call_model(prompt):
    """经 HERMES_RUN_CMD 模板发起单轮调用, 返回 (rc, 输出文本)。"""
    tpl = os.environ.get("HERMES_RUN_CMD") or 'hermes --yolo -z "{PROMPT}"'
    if "{PROMPT}" not in tpl:
        return 2, "HERMES_RUN_CMD 缺 {PROMPT} 占位符"
    cmd = tpl.replace("{PROMPT}", prompt.replace('"', "」"))
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           errors="replace", timeout=CALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        return 124, "超时"
    except OSError as exc:
        return 1, str(exc)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def parse_kv_output(text):
    """模型输出 → 记录 dict(嵌套 source.*, 数字/枚举宽松校验)。"""
    rec = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line or line.startswith("#"):
            continue
        k, v = line.split("=", 1)
        k = k.strip().lower()
        v = v.strip()
        if not re.match(r"^[a-z_]+$", k):
            continue
        if v == "":
            continue
        rec[k] = v
    # 嵌套 source.*
    source = {}
    for k in [k for k in rec if k.startswith("source_")]:
        source[k[len("source_"):]] = rec.pop(k)
    if source:
        rec["source"] = source
    return rec


def validate(rec, url):
    """最小校验: 标题/URL/quote 三硬项 + quote 长度。返回错误或 None。"""
    src = rec.get("source") or {}
    title = rec.get("title_cn") or rec.get("title_en")
    if not title:
        return "缺标题"
    if not (src.get("url") or rec.get("url")):
        return "缺 url"
    if src.get("url") and url and src["url"].rstrip("/") != url.rstrip("/") \
            and src["url"] not in url and url not in src["url"]:
        return "url 与存档不符(%s)" % src["url"][:60]
    quote = src.get("quote") or ""
    if quote and len(quote) > 120:
        src["quote"] = quote[:120]  # 截断而非拒收(保守可入库)
    if not quote:
        src["quote"] = (rec.get("title_cn") or "")[:100] or "(标题代quote)"
    rec.setdefault("record_type", "policy")
    return None


def extract(date_str, log=print):
    today = biz_today()
    raw_dir = os.path.join(DATA_DIR, "raw", date_str)
    out_path = os.path.join(raw_dir, "extracted-policy.jsonl")
    skip = done_urls(date_str)
    archives = [a for a in load_archives(date_str) if a[2] and a[2] not in skip]
    if not archives:
        log("[extract_records] %s: 无待抽取存档(共 %d 份, 已抽 %d)"
            % (date_str, len(load_archives(date_str)), len(skip)))
        return 0, 0
    n_ok = n_fail = 0
    log("[extract_records] %s: 待抽取 %d 份" % (date_str, len(archives)))
    for mp, meta, url, body in archives:
        prompt = build_prompt(meta, url, body, today)
        rec = None
        for attempt in (1, 2):
            rc, out = call_model(prompt)
            if rc == 0:
                cand = parse_kv_output(out)
                err = validate(cand, url)
                if err is None:
                    rec = cand
                    break
                last = "输出不合规格: %s" % err
            else:
                last = "调用失败 rc=%s: %s" % (rc, _clean(out)[:120])
                if "429" in out or "Token Plan" in out:
                    # 预算耗尽: 快速失败, 剩余文档不烧重试
                    log("[extract_records][WARN] Token Plan 耗尽, 剩余 %d 份留待下轮"
                        % (len(archives) - n_ok - n_fail - 1))
                    _log_line(raw_dir, "| 抽取 | 中断 | Token Plan 耗尽(429) |")
                    return n_ok, n_fail + 1
        if rec is None:
            n_fail += 1
            _log_line(raw_dir, "| 抽取 | 失败 | %s %s |" % (url[:70], _clean(last)[:60]))
            log("[extract_records][WARN] 失败: %s (%s)" % (url[:70], _clean(last)[:80]))
            continue
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        n_ok += 1
        log("[extract_records] ok: %s" % (rec.get("title_cn") or url)[:70])
    return n_ok, n_fail


def _log_line(raw_dir, line):
    try:
        with open(os.path.join(raw_dir, "search-log.md"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def writeback_risk(date_str, log=print):
    """normalize 之后: 对当日新入库且带研判字段的记录, 确定性写回 risk.py。

    匹配: 抽取记录的 source.url → 当日 events(impact_level 为空)的 url → event_id。
    表结构实测(2026-10-25): first_seen 在 payload JSON 里、impact_level 是表列 ——
    初版查询用错列名抛 sqlite3.OperationalError 且被调用方 || true 吞掉, 5 条
    记录研判静默缺失, 本版修正并自带异常留痕。
    """
    import sqlite3
    from normalize import DB_PATH
    raw_path = os.path.join(DATA_DIR, "raw", date_str, "extracted-policy.jsonl")
    if not os.path.isfile(raw_path):
        return
    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True)
        rows = con.execute(
            "SELECT event_id, json_extract(payload, '$.source.url') FROM events "
            "WHERE impact_level IS NULL "
            "AND json_extract(payload, '$.first_seen') LIKE ?",
            (date_str + "%",)).fetchall()
        con.close()
    except sqlite3.Error as exc:
        log("[extract_records][WARN] 研判写回查询失败(留待 risk.py --list-pending 人工): %s" % exc)
        return
    by_url = {u: eid for eid, u in rows if u}
    n = 0
    with open(raw_path, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            src = rec.get("source") or {}
            eid = by_url.get(src.get("url"))
            if not eid:
                continue
            dims = {}
            for k, arg in (("dim_trade", "trade"), ("dim_biosecurity", "biosecurity"),
                           ("dim_response", "response"), ("dim_alignment", "alignment")):
                if re.match(r"^[0-5]$", str(rec.get(k, ""))):
                    dims[arg] = rec[k]
            if len(dims) != 4 or not rec.get("impact_type") or not rec.get("china_relevance"):
                continue  # 研判字段不全 → 留给 --list-pending, 不硬写
            args = [sys.executable, os.path.join(REPO, "scripts", "risk.py"),
                    "--event-id", eid,
                    "--impact-type", rec["impact_type"],
                    "--china-relevance", rec["china_relevance"],
                    "--action", (rec.get("summary_cn") or "")[:60],
                    "--rationale", (rec.get("impact_rationale") or "文档上下文研判")[:200]]
            for arg, v in dims.items():
                args += ["--dim", "%s=%s" % (arg, v)]
            p = subprocess.run(args, capture_output=True, text=True, timeout=60)
            if p.returncode == 0:
                n += 1
            else:
                log("[extract_records][WARN] risk 写回失败 %s: %s"
                    % (eid, _clean(p.stdout or p.stderr)[:80]))
    if n:
        log("[extract_records] 研判写回 %d 条" % n)


def main():
    ap = argparse.ArgumentParser(description="单轮微调用抽取(每份存档一次 LLM 调用)")
    ap.add_argument("--date", default=biz_today(), help="业务日期 YYYY-MM-DD")
    ap.add_argument("--stage", choices=["extract", "risk"], default="extract",
                    help="extract=抽取(默认); risk=normalize 之后的研判写回(由 run_scan.sh 编排先后)")
    args = ap.parse_args()
    if args.stage == "risk":
        writeback_risk(args.date)
        return 0
    n_ok, n_fail = extract(args.date)
    print("[extract_records] 完成: 成功 %d / 失败 %d" % (n_ok, n_fail))
    return 0


if __name__ == "__main__":
    sys.exit(main())
