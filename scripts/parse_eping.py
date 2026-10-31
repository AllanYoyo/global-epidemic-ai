#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 ePing 通报数据里筛出「窗口内 + 疑似政策动作」的精简清单。

为什么要有这个脚本(2026-10-22 生产现场):
  ePing 的列表数据每条自带 **发布日期(dist)**、**文号(sym)**、**成员(member)**、
  **措施关键词(kw)** 与**原文 PDF 直链(crn)**。但 10-22 的 Agent 拿到这份数据后,
  **没有用 dist/kw 做预筛**, 而是逐条下载 PDF 去核发布日期 —— 存了 23+ 个文件、
  跑了 40 分钟没完(且期间 execute_code 连报 12 次错, 是典型的"自己写解析脚本
  调试循环", 每重试一次就是一次 LLM 往返 + 一次外部服务额度)。

  本脚本把这一步固化: **一条命令, 零额度消耗, 零调试**。
  输入可以是 Agent 从列表页拿到的 JSON, 也可以是"成员|日期|文号|标题|直链"
  的行式文本(10-21 那轮就是这种形状)。

用法:
  python scripts/parse_eping.py <数据.json|数据.txt> [--days 7] [--date 2026-10-22]
  python scripts/parse_eping.py <数据.json> --all          # 不按动作预筛, 只按日期

输出(逐行):
  成员 | 发布日期 | 文号 | 措施关键词 | 标题(截断) | PDF/原文直链
同时把精简结果另存为 <数据>.filtered.json, 供后续抽取直接读。
"""
import argparse
import datetime
import json
import os
import re
import sys
import unicodedata

# 业务时区固定东八区: 不依赖宿主机 TZ(生产 VPS 系统时区是 Etc/UTC)。
# 与 scripts/normalize.py 的 biz_today() 同源, 避免各处各算一遍日期。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from normalize import biz_today
except ImportError:  # 独立运行(不在仓库内)时的兜底
    def biz_today():
        return (datetime.datetime.utcnow()
                + datetime.timedelta(hours=8)).date().isoformat()


def _use_utf8_stdout():
    """Windows 控制台默认 GBK, 印 ePing 的西/葡/法文标题会 UnicodeEncodeError。

    生产是 Linux(UTF-8), 这里只是让本机开发/调试不再被编码问题打断。
    """
    for stream in (sys.stdout, sys.stderr):
        enc = getattr(stream, "encoding", "") or ""
        if enc.lower().replace("-", "") in ("utf8",):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# 疑似"措施变化"的关键词(命中其一即视为候选)。来源: ePing 的 kw 字段实测取值
# ('Withdrawal of the measure' / 'Import ban' / 'Emergency measure' ...)。
ACTION_HINTS = (
    "emergency", "import ban", "ban on import", "suspension", "suspend",
    "withdrawal", "lift", "restriction", "limitation", "establish", "revision",
    "amend", "requirement", "phytosanitary", "quarantine", "sanitary",
    "surveillance", "eradication", "infestation", "detection", "control measure",
    "additional", "modification", "declaration", "addendum", "revocation",
)
# 非英语的措施动词/主题词(ePing 标题多用各成员本国语言: 西/葡/法/俄 ...)。
# 只在**标题**上匹配, 不碰 kw —— kw 是 WTO 的英文分类词, 词面稳定。
# 词族按"词根"取, 以便同时覆盖西/葡/法变体(西葡同源词差异主要在元音)。
EXTRA_HINTS = (
    # 措施动作
    "medida", "suspension", "suspensao", "suspender", "prohibicion", "proibicao",
    "proibi", "restriccion", "restricao", "restri", "emergencia", "revocacion",
    "retirada", "retirar", "rectificacion", "alerta",
    # 检疫主题
    "fitossanitari", "fitosanitari", "phytosanitair", "veterinari", "quarentena",
    "sanitari", "zoosanitari", "certificad", "inspec",
    # 文件性质
    "revision", "revisao", "requisit", "exigencia", "alteracao", "modificacao",
    "adenda", "rectific", "norma",
    # 法语
    "alerte", "interdiction", "mesure", "releve", "obligation", "importation",
    "exportation", "revision", "exigence",
)
# 明显不是措施变化的关键词(纯背景/通报性质), 命中则降权。
NEGATIVE_HINTS = ("comment", "consultation", "notification of", "circular", "news")
# 行式列表里的文号(G/SPS/N/...)与日期。行式产物形状会随 Agent 的解析写法漂移,
# 所以按内容判定而不是固定下标(2026-10-22 生产实机验证的教训)。
_SYM_RE = re.compile(r"^((?:G/[A-Z]+/N/|N/)[^\s]+)(?:\s+(.*))?$", re.S)
_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})\b")


def _fold(s):
    """去音标 + 小写。

    ePing 标题里 'suspensión'(西)/'suspensão'(葡)带音标, 直接用英文关键词
    匹配会漏掉 —— 这是 10-22 预筛逻辑的一个隐性漏检点。
    """
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return s.lower()


def _first(rec, *names):
    for n in names:
        v = rec.get(n)
        if v not in (None, "", [], {}):
            return v
    return None


def _dist(rec):
    """发布日期, 归一成 YYYY-MM-DD。"""
    v = _first(rec, "dist", "distributionDate", "publicationDate", "date",
               "published", "publishDate", "notifiedDate")
    if not v:
        return None
    s = str(v).strip()
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return "%s-%s-%s" % m.groups()
    # 21/10/2026 这类
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        d, mo, y = (int(x) for x in m.groups())
        return "%04d-%02d-%02d" % (y, mo, d)
    return None


def _pdf(rec):
    """从附件字段里抠出归档用的原文直链。

    优先级(2026-10-22 修正): WTO 存档 PDF > 任意 PDF > 附件下载页。
    原实现用一条正则把 'getattachment' 和 '.pdf' 混在一起, 且非贪婪匹配,
    实际先命中的是成员国的附件下载页(如 ica.gov.co/getattachment/...aspx),
    而 skill 明确要求走 members.wto.org/crnattachments 的存档 PDF。
    2026-10-31 补: ePing 官方 API(api.wto.org/eping)的字段名是
    notifiedDocumentLink(通报 PDF 直链, 可能逗号连着多个)与 dolLink(DOS 路径),
    一并纳入扫描。
    """
    raw = _first(rec, "crn", "notifiedDocumentLink", "dolLink",
                 "documents", "attachments", "link", "url") or ""
    if isinstance(raw, (list, tuple)):
        raw = " ".join(str(x) for x in raw)
    raw = str(raw)
    # 1) WTO 官方存档 PDF(官方域白名单内, tier 1 可溯源)
    m = re.search(r"https?://members\.wto\.org/crnattachments[^\s\"'<>]+", raw, re.I)
    if m and ".pdf" in m.group(0).lower():
        return m.group(0)
    # 2) 任意 PDF
    m = re.search(r"https?://[^\s\"'<>]+?\.pdf", raw, re.I)
    if m:
        return m.group(0)
    # 3) 附件下载页兜底
    m = re.search(r"https?://[^\s\"'<>]*getattachment[^\s\"'<>]*", raw, re.I)
    if m:
        return m.group(0)
    return ""


def _kw(rec):
    v = _first(rec, "kw", "keywords", "spsKeywords", "subjects", "tags", "categories")
    if isinstance(v, (list, tuple)):
        # 2026-10-31 补: ePing 官方 API 的 keywords/spsKeywords 是对象数组
        # [{id, name}], 取 name 拼接(旧实现 str(list) 会把整个 dict 拼进去)
        parts = []
        for x in v:
            if isinstance(x, dict):
                parts.append(str(x.get("name") or x.get("code") or ""))
            else:
                parts.append(str(x))
        return " / ".join(p for p in parts if p)
    return str(v or "")


def _is_action(rec):
    """是否疑似措施变化。

    判定顺序:
    1. 否定词(评议期/通知/通函类)且不含硬动作词 → 排除;
    2. 命中动作词(英文词面) → 候选;
    3. 命中多语言词族(只在标题上) → 候选。

    第 2 步同时看 kw 与标题: 行式列表**没有关键词列**(kw 为空),
    只看 kw 的话标题里的 "Import ban" / "Establishes ..." 会被漏掉。
    """
    kw = _fold(_kw(rec))
    title = _fold(_first(rec, "title", "title_cn", "description", "desc") or "")
    hard = ("ban", "suspension", "emergency", "withdrawal", "restriction")
    hay = kw + " " + title
    if any(h in hay for h in NEGATIVE_HINTS) and not any(h in hay for h in hard):
        return False
    if any(h in hay for h in ACTION_HINTS):
        return True
    return any(h in title for h in EXTRA_HINTS)


def _from_json(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        for key in ("results", "data", "items", "notifications", "rows", "records"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]
    out = []
    for rec in data:
        if not isinstance(rec, dict):
            continue
        out.append({
            "member": str(_first(rec, "member", "notifyingMember", "notifying_member", "country") or "-"),
            "dist": _dist(rec),
            "sym": str(_first(rec, "sym", "documentSymbol", "symbol", "notificationSymbol") or "-"),
            "title": str(_first(rec, "title", "title_cn", "description", "desc") or ""),
            "kw": _kw(rec),
            "url": _pdf(rec),
            "area": str(_first(rec, "area", "domain", "spTbtArea") or ""),
            "raw": rec,
        })
    return out


def _from_text(path):
    """行式文本: 10-21 生产实测形状。

    真实产物 `sps_list.txt`(2026-10-21, 26 行)每一行是:
        序号 | 成员 | 日期 | 文号+标题 | 直链
    例:
        0 | Colombia | 21/10/2026 | G/SPS/N/COL/396/Add.1 Resolución ICA 00020296 ... | https://members.wto.org/crnattachments/2026/SPS/COL/26_05465_00_s.pdf

    两处与旧文档不一致的地方(2026-10-22 生产实机验证才发现, 旧实现全军覆没):
    1. **行首有序号列**, 成员在第 2 段而不是第 1 段;
    2. **文号与标题合并在一段里**(空格分隔), 没有独立标题列;
       而且日期段可能有**两个**日期 —— 第一个是发布日(dist), 第二个通常是
       生效日(如 Costa Rica "20/10/2026 19/12/2026"), 对抽取 effective_date 有用。

    所以按内容判定每个字段, 不按固定下标。
    """
    out = []
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.rstrip()
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 3:
                continue
            url = ""
            dates = []
            sym = ""
            title = ""
            rest = []
            for p in parts:
                if not p:
                    continue
                low = p.lower()
                if "http://" in low or "https://" in low:
                    url = url or p
                    continue
                m = _SYM_RE.match(p)
                if m:
                    sym = m.group(1)
                    title = (m.group(2) or "").strip()
                    continue
                found = _DATE_RE.findall(p)
                if found:
                    if not dates:
                        dates = found
                    continue
                rest.append(p)
            # rest: 纯数字是序号, 第一个非数字段是成员, 其余是标题(旧 5 段形状)
            member = ""
            title_parts = []
            for p in rest:
                if not member and not p.isdigit():
                    member = p
                    continue
                if member:
                    title_parts.append(p)
            if not title:
                title = " ".join(title_parts)
            rec = {"member": member or "-", "sym": sym or "-", "title": title,
                   "dates": dates, "url": url}
            rec["dist"] = _dist({"dist": dates[0]}) if dates else None
            out.append({"member": rec["member"], "dist": rec["dist"], "sym": rec["sym"],
                        "title": rec["title"], "kw": "", "url": rec["url"],
                        "dates": dates, "raw": rec})
    return out


def main():
    _use_utf8_stdout()
    ap = argparse.ArgumentParser(description="ePing 通报预筛(按发布日期 + 措施关键词)")
    ap.add_argument("source", help="数据文件(.json 列表 / .txt 行式文本)")
    ap.add_argument("--days", type=int, default=7, help="回看窗口天数(默认 7)")
    ap.add_argument("--date", default=None, help="业务日期 YYYY-MM-DD(默认今天, 东八区)")
    ap.add_argument("--all", action="store_true", help="不按措施关键词预筛, 只按日期")
    ap.add_argument("--limit", type=int, default=12, help="最多输出多少条(默认 12)")
    args = ap.parse_args()

    if not os.path.isfile(args.source):
        print("[错误] 文件不存在: %s" % args.source)
        return 2
    day = args.date or biz_today()
    try:
        base = datetime.date.fromisoformat(day)
    except ValueError:
        print("[错误] --date 格式应为 YYYY-MM-DD")
        return 2
    floor = base - datetime.timedelta(days=args.days)

    try:
        rows = _from_json(args.source) if args.source.lower().endswith(".json") else _from_text(args.source)
    except Exception as exc:
        print("[错误] 解析失败: %s" % exc)
        return 2

    if not rows:
        print("[提示] 没有解析出条目: %s" % args.source)
        return 1

    dated, undated, chosen = [], [], []
    for r in rows:
        if not r["dist"]:
            undated.append(r)
            continue
        try:
            d = datetime.date.fromisoformat(r["dist"])
        except ValueError:
            undated.append(r)
            continue
        if floor <= d <= base:
            dated.append(r)
    for r in dated:
        if args.all or _is_action(r):
            chosen.append(r)

    print("共解析 %d 条; 发布日期在窗口(%s ~ %s)内 %d 条; 疑似措施变化 %d 条"
          % (len(rows), floor, base, len(dated), len(chosen)))
    if undated:
        print("  (另有 %d 条无发布日期, 未计入窗口 —— 需要人工判断时再看)" % len(undated))

    chosen.sort(key=lambda r: r["dist"] or "", reverse=True)
    shown = chosen[:args.limit]
    print("")
    for r in shown:
        print("%s | %s | %s | %s | %s | %s"
              % (r["member"], r["dist"] or "-", r["sym"],
                 (r["kw"] or "-")[:60],
                 (r["title"] or "")[:70].replace("\n", " "),
                 r["url"] or "-"))
    if len(chosen) > args.limit:
        print("")
        print("… 共 %d 条候选, 本次只列前 %d 条(上限 --limit)。"
              "**先按这份清单挑要存档的条目, 不要逐条把 %d 条全下下来。**"
              % (len(chosen), args.limit, len(chosen)))

    out_path = args.source.rsplit(".", 1)[0] + ".filtered.json"
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump([r["raw"] for r in chosen], f, ensure_ascii=False, indent=2)
        print("")
        print("精简结果已存: %s" % out_path)
    except OSError as exc:
        print("[WARN] 精简结果写入失败: %s" % exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
