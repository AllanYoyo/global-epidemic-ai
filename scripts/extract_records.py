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
is_policy_change=<yes|no>
non_policy_note=<若 is_policy_change=no 写明类别(会议/表彰/装运/栏目页/解读等), 否则留空>
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

# ---- 收录范围规则(2026-10-27 补)----
# 为什么补: 10-25~27 连续三天, 同一批非政策内容被当成政策入库 —— 表彰福布斯 3 份、
# CPM 局会议 3 份、首批输华牛肉启运 3 份、土耳其栏目页/IPPC 活动页各 1 份。
# 根因: Agent 路径的 prompts/policy-extraction.md 有"只收变化"和"目录页"规则, 但
# P4 管线的 prompt 只有 FIELD_TEMPLATE(纯字段清单), **规则根本没进上下文** ——
# 与 AGENTS.md 记的 92dbe1e 缺口同源(字段规则既不在上下文里又不许读)。
# 现在把规则内联进 prompt, 并在脚本侧加确定性闸门(模型自判 + 关键词兜底)。
SCOPE_RULES = """【收录范围(硬规则, 先判后抽)】
本系统只监测**政策/措施的规则变化**:出台、修订、暂停、恢复、放宽、延期、准入/登记
规则调整、标准/规范发布或公开征求意见、证书与标识要求、名单增删。
先判断这段原文是不是政策变化, 再抽字段。is_policy_change=yes 只在原文能指出
「哪条规则变了、变成什么」时才填; 否则填 no, 并在 non_policy_note 写明类别。
下列内容**一律 is_policy_change=no**(哪怕它出现在官方网站的新闻里):
- 会议与活动:会议/峰会/论坛/研讨会/座谈会/理事会/年会/大会/磋商期/活动公告/开幕/
  闭幕/培训/参观/考察/调研 —— 会议本身不是措施, 会议通知更不是;
- 领导与礼宾活动:出席/会见/接待/访问/陪同/见证/致辞/贺电/贺信/慰问/悼念/奠基/
  揭牌/剪彩/落成/启动仪式/启运仪式/装船/首发 —— 领导出席不等于政策生效;
- 荣誉与表彰:表彰/颁奖/获奖/入选榜单/百强榜/荣誉/嘉奖 —— 纯人物与排行新闻;
- 栏目页与非正文页:栏目/专栏/导航/列表页/专题页/活动页/公告页/首页, 以及摘录只
  有导航、脚本、关键词元数据、DataTable 之类的页面 —— 页面本身不是政策;
- 解读与宣传:政策解读/宣讲/科普/综述/盘点/答记者问/宣传推广;
- 单纯的贸易与履约事件:首批装运/到港/成交/发货/签署仪式, 除非原文同时给出
  随之生效的准入或检疫规则(那要把规则本身抽出来, 而不是抽新闻)。
若 is_policy_change=no, 仍然输出其余字段, 但本条会被脚本拒收不入库。"""

PROMPT_TPL = """你是动植物检疫政策抽取器。从下面这份政策原文摘录里抽取一条政策记录。
只输出 key=value 行(每行一个字段, 值内不要换行), 不输出任何解释、标题或代码块。
%s
字段与取值要求如下(%s 为发布日期, 超过 7 天的旧文件仍照实抽取, 入库端会拦):
%s
【存档元信息】
url=%s
title=%s
【原文摘录(逐字, 已截断)】
%s
现在只输出 key=value 行。"""


# ---- 非政策内容筛查(确定性闸门)----
# 第一道: 模型自判 is_policy_change=no(语义判断交给 LLM, 准确率最高)。
# 第二道: 模型自述"整段没有措施/只有页面元数据"这类**整体性否定** → 判非政策。
# 第三道: 关键词兜底 —— 命中"非政策信号"且**没有**"规范动作"才判非政策。
#
# 口径校准(2026-10-27 生产实测, 15 条候选里有一条真政策差点被误杀):
#   · 否定句必须针对**整段内容**, 不能针对"检疫"二字 —— 实测
#     「美国禁止进口部分加拿大乳制品并维持50%关税(属贸易争端措施而非检疫措施)」
#     是**真政策**, 若把"非检疫措施"当否定句就会误杀。所以否定层只收
#     "不含任何/未见任何/仅含页面元数据/无可抽取的政策动作"这类整体性表述;
#     "未见具体检疫措施/非检疫措施"这类只谈检疫维度的表述**不算**否定。
#   · 规范动作必须是「变更动词 + 政策对象」**同时**出现 —— 只出现"检疫/措施"
#     这类领域词不足以说明动了规则(表彰新闻里也会写"非检疫措施")。
# 第三道刻意保守: 宁可放过(留待核验环节处理), 也不误杀真政策 —— 误杀会直接
# 丢掉一条真政策, 放过只是日报里多一条待核实项。
NON_POLICY_SIGNALS = (
    # 会议与活动
    "会议", "会展", "论坛", "研讨会", "座谈会", "交流会", "招待会", "理事会", "年会",
    "开幕", "闭幕", "讲座", "培训", "参观", "考察", "调研", "视察", "磋商", "研讨",
    # 领导与礼宾
    "出席", "会见", "接待", "访问", "陪同", "见证", "慰问", "悼念", "吊唁", "祝贺",
    "贺电", "贺信", "致辞", "讲话", "揭牌", "揭幕", "剪彩", "奠基", "落成", "仪式",
    "启运", "装船", "首发", "启幕", "到港", "抵达", "成交", "发货",
    # 荣誉与表彰
    "颁奖", "表彰", "获奖", "榜单", "百强", "荣誉", "嘉奖", "当选", "入选",
    # 栏目页与非正文页
    "栏目", "专栏", "导航", "列表页", "专题页", "活动页", "公告页", "首页", "专题",
    "前瞻", "预告", "快讯", "简讯",
    # 解读与宣传
    "解读", "宣讲", "宣传", "推广", "科普", "综述", "盘点", "答记者问", "采访",
    "报道", "喜讯",
)
# 变更动词: 说明"动了规则"的动词性信号
CHANGE_VERBS = (
    "禁止", "禁运", "暂停", "取消", "恢复", "放宽", "加严", "修订", "修正", "修改",
    "发布", "实施", "生效", "废止", "新增", "纳入", "允许", "准入", "解除", "加设",
    "提高", "下调", "上调", "限制", "要求", "调整", "收紧", "简化", "开放", "登记",
    "注册", "签发", "强制", "试行", "延长", "延期", "征求意见", "磋商期", "征询",
    "公开征求", "授权", "明确",
)
# 政策对象: 说明"动的是哪一类规则"
POLICY_OBJECTS = (
    "检疫", "疫区", "进口", "出口", "准入", "证书", "标识", "名单", "名录", "清单",
    "标准", "规范", "登记", "注册", "许可", "关税", "配额", "协议", "公约", "法规",
    "条例", "决议", "公告", "通告", "措施", "卫生", "口岸", "货物", "产品", "种苗",
    "肉类", "禽类", "乳制品",
)
# 弱动词: 只有与**特定对象**搭配时才算规范动作。
# 「通过」就是典型 —— 「会议通过新标准」是政策, 但「该批货物已通过检疫」是履约
# 事实(装运类新闻), 不能因为"通过+检疫"就放行。
WEAK_VERBS = {
    "通过": ("标准", "规范", "措施", "决议", "法规", "条例", "协议", "议定书", "修正案"),
}
# 文号形态(第247号令 / Portaria 962/2026 / No. 123): 强规范信号
NORMATIVE_PATTERNS = (
    r"第\s*\d+\s*号", r"[Nn]o\.?\s*\d+", r"[Nn]º\.?\s*\d+", r"\bG/SPS/N/", r"\bISPM\s*\d+",
)
# 整体性否定: 说明**整段内容**里没有政策动作(模型自述), 直接判非政策。
# 刻意不含"未见具体检疫措施/非检疫措施"这类只谈检疫维度的表述(会误杀贸易措施)。
NEGATION_SIGNALS = (
    "不含任何", "未含任何", "未见任何", "摘录仅含", "仅含页面", "仅含关键词",
    "关键词元数据", "页面关键词", "无相关措施", "未见措施", "无可抽取的政策动作",
    "未含可核验政策正文", "无可核验", "未见可核验的政策动作", "无政策正文",
    "仅为表彰", "仅为脚本", "初始化代码", "属会议安排", "属机构运行安排",
    "机构运行安排", "议程安排", "治理议程", "会议安排", "会务安排", "议事安排",
    "属机构运行",
)



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
    """已抽取过的 url 集合(extracted-policy.jsonl 逐行) + 已拒收的 url(rejected-policy.jsonl)。

    拒收的也要算"已处理": 否则同一篇会议/表彰新闻明天会被重新抽取一次,
    白烧一次 LLM 调用, 还会反复占用当日存档配额。
    """
    urls = set()
    raw_dir = os.path.join(DATA_DIR, "raw", date_str)
    for name in ("extracted-policy.jsonl", "rejected-policy.jsonl"):
        p = os.path.join(raw_dir, name)
        if not os.path.isfile(p):
            continue
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if name == "extracted-policy.jsonl":
                    urls.add((row.get("source") or {}).get("url") or row.get("url"))
                else:
                    urls.add(row.get("url"))
    return {u for u in urls if u}


def build_prompt(meta, url, body, today):
    return PROMPT_TPL % (SCOPE_RULES, today, FIELD_TEMPLATE, _clean(url),
                         _clean(meta.get("title") or "")[:120],
                         _clean(body) if body else "(正文不可读, 仅凭标题与URL抽取, quote 填标题)")


def _norm_flag(v):
    """is_policy_change 取值归一: yes/no/是/否/true/false。"""
    s = str(v or "").strip().lower()
    if s in ("no", "否", "false", "n", "0"):
        return "no"
    if s in ("yes", "是", "true", "y", "1"):
        return "yes"
    return ""


def screen_non_policy(rec):
    """非政策内容筛查 —— 返回 (是否拒收, 原因)。三道闸门, 从严到宽。

    ① 模型自判 is_policy_change=no → 拒收(模型自己说这不是政策变化);
    ② 模型自述"整段没有措施/只有页面元数据" → 拒收(整体性否定最可信);
    ③ 关键词兜底: 命中"非政策信号"且**全文没有**"规范动作" → 拒收。
       规范动作 = 变更动词 + 政策对象 **同时**出现(或命中文号形态)。
    """
    flag = _norm_flag(rec.get("is_policy_change"))
    if flag == "no":
        note = str(rec.get("non_policy_note") or "").strip()
        return True, "模型判定非政策内容%s" % (("(%s)" % note[:40]) if note else "")

    # 可用于判定的文本: 标题 + 摘要 + 原文引用(引用是原文原话, 信号最强)
    src = rec.get("source") or {}
    parts = [str(rec.get("title_cn") or ""), str(rec.get("title_en") or ""),
             str(rec.get("summary_cn") or ""), str(src.get("quote") or "")]
    text = " ".join(p for p in parts if p)
    if not text.strip():
        return False, ""

    for sig in NEGATION_SIGNALS:
        if sig in text:
            return True, "模型自述整段无措施(%s)" % sig

    non_hit = [s for s in NON_POLICY_SIGNALS if s in text]
    if not non_hit:
        return False, ""
    if _has_normative_action(text):
        # 有规范动作 → 可能是"会议通过/宣布了某措施", 放行
        return False, ""
    return True, "非政策信号(%s)且无规范动作" % "、".join(non_hit[:3])


def _has_normative_action(text):
    """是否含规范动作: 变更动词 + 政策对象(同时出现), 或命中文号形态。"""
    if any(re.search(p, text) for p in NORMATIVE_PATTERNS):
        return True
    has_verb = any(v in text for v in CHANGE_VERBS)
    has_obj = any(o in text for o in POLICY_OBJECTS)
    if has_verb and has_obj:
        return True
    # 弱动词: 只在与特定对象搭配时成立(如「通过标准」成立, 「通过检疫」不成立)
    for verb, objects in WEAK_VERBS.items():
        if verb in text and any(o in text for o in objects):
            return True
    return False


def _write_rejected(raw_dir, rec, url, reason):
    """拒收记录落盘(rejected-policy.jsonl) —— 既留审计痕迹, 也避免明天重抽同一页。"""
    path = os.path.join(raw_dir, "rejected-policy.jsonl")
    row = {"url": url, "title": rec.get("title_cn") or rec.get("title_en") or "",
           "reason": reason, "source": rec.get("source") or {}}
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


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
    n_ok = n_fail = n_reject = 0
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
        # 非政策内容闸门(2026-10-27): 会议/表彰/装运/栏目页等不入库
        reject, why = screen_non_policy(rec)
        if reject:
            n_reject += 1
            _write_rejected(raw_dir, rec, url, why)
            _log_line(raw_dir, "| 抽取 | 拒收 | 非政策内容 %s %s |"
                      % (why[:40], url[:60]))
            log("[extract_records] 拒收(非政策内容): %s (%s)"
                % ((rec.get("title_cn") or url)[:60], why))
            continue
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        n_ok += 1
        log("[extract_records] ok: %s" % (rec.get("title_cn") or url)[:70])
    if n_reject:
        log("[extract_records] 本轮拒收非政策内容 %d 份(已写 rejected-policy.jsonl)" % n_reject)
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
