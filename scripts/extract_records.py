#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""疫见全球 · 单轮微调用抽取器(2026-10-25 P4 引入)—— 每份存档一次 LLM 调用

为什么需要它: Agent 会话式抽取对"轮次上限/Token 耗尽/阶段超时"任何一种死法
都是整轮全损(10-25 三连败实证)。本脚本把抽取切成**每份存档一次单轮调用**:

  对每个已存档但未抽取的 meta.json → 剥 head 后取正文摘录(≤8KB) + 字段模板拼 prompt
  → **直连 Anthropic 兼容端点**发起单轮调用(无工具、无仓库上下文注入;
    端点不可用时才回落 HERMES_RUN_CMD 的 CLI 通道)
  → 模型输出 key=value 行 → 本地解析校验 → 追加 extracted-policy.jsonl

  2026-11-18 起默认走直连通道 + 6 路并发: 加工段 36 分 45 秒 → 约 1~2 分钟。
  复盘与选型依据见文件头 EXTRACT_* 配置块。

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
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from normalize import biz_today, biz_now, DATA_DIR
import scope_guard  # 收录口径红线(动植物检疫 vs 食品安全)的单一判定来源

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CALL_TIMEOUT = 150          # 单次微调用上限(秒)
SNIP_BYTES = 4096           # 每份文档进 prompt 的摘录上限(实际窗口 = SNIP_BYTES*2 字符)
FILE_READ_BYTES = 1 << 20   # 单份存档最大读取字节(生产实测最大 ~340KB, 1MB 兜极端页)

# ---- 抽取通道配置(2026-11-18 性能事故后引入, 全部可用环境变量覆盖)----
#
# 为什么要换通道(2026-11-18 生产复盘, 45 分钟那轮):
#   抽取段只需要"一次确定性文本补全", 却用 Agent CLI(hermes --yolo -z)去跑,
#   每份存档白付四笔成本:
#     ① CLI 每次注入整个仓库上下文(AGENTS.md + skills + hermes-cli 工具集 schema)
#        —— 实测 74 次抽取 in=1,043,779 token, 我们自己发的 prompt 只占 89,600,
#        **91% 是注入的上下文**;
#     ② CLI 进程启动 ~5s/份;
#     ③ 拿到了全套工具(terminal/read_file/web_extract/execute_code) + max_turns=50
#        —— 2026-11-16 全局模型换成 MiniMax-M3.1-Flash 后, 22% 的调用脱离
#        "单轮"契约跑成 Agent 工具循环(均 70.7s, 最长 257.6s), 把加工段从
#        7 分钟拖到 36 分 45 秒; 模型还真的去读 scope_guard.py、查生产库
#        epidemic.db、web_extract 抓外网 —— 数据面拿到了执行面;
#     ④ 折合 171 万 token/日, 而直连只要约 13 万(8%)。
#   直连 Anthropic 兼容端点 = 无工具(结构上不可能跑偏) + 输入只有我们自己的
#   prompt + 无启动开销 + 可并发。
#
# max_tokens 为什么给到 8000: 该端点把**思考 token 也算进输出预算**
# (实测 output_tokens_details.thinking_tokens≈1100), 模型可用正文只剩
# 剩下的部分 —— 给 1600/3000 都会在长记录上被截断(截断 = 字段缺失 =
# validate 失败 = 重试 = 更慢)。这是 M3.1-Flash 2/6 截断的直接成因。
#
# 模型钉死: MiniMax-M3.1-Flash(2026-11-18 用户拍板 —— M2.5 将被淘汰, 抽取段
# 必须在 M3.1 上跑通)。M3.1 的两个特性必须一起对付:
#   ① **输出冗长**: 实测 M2.5 的 out≈1026, M3.1 的 out≈2856(同一份 prompt);
#   ② **思考 token 也吃输出预算**: 实测 output_tokens_details.thinking_tokens
#      ≈800~1100, 即"正文可用预算 = max_tokens − 思考 token"。
#   截断的后果很重: 模型若在写正文前就把预算烧光 → 输出里一个 key=value 都没有
#   (实测 kv=0) → validate 判"缺标题" → 重试 → 整份报废还多烧一次。
#   对付手段(按实测效果定档): max_tokens 给足 + prompt 硬约束"第一行就是
#   record_type=policy"+ 失败重试时加预算重问(见 call_model 的 max_tokens 覆盖)。
EXTRACT_BACKEND = os.environ.get("EXTRACT_BACKEND", "auto").strip().lower()  # auto|api|cli
EXTRACT_MODEL = os.environ.get("EXTRACT_MODEL", "MiniMax-M3.1-Flash").strip()
EXTRACT_API_BASE = os.environ.get("EXTRACT_API_BASE_URL",
                                  "https://api.minimaxi.com/anthropic").rstrip("/")
EXTRACT_API_KEY_ENV = os.environ.get("EXTRACT_API_KEY_ENV", "MINIMAX_CN_API_KEY")
EXTRACT_MAX_TOKENS = int(os.environ.get("EXTRACT_MAX_TOKENS", "8000") or 8000)
EXTRACT_TIMEOUT = int(os.environ.get("EXTRACT_TIMEOUT", "90") or 90)
EXTRACT_WORKERS = int(os.environ.get("EXTRACT_WORKERS", "6") or 6)
# CLI 兜底通道是否强制 --safe-mode(数据面不该有工具)。实测: 去掉 --yolo 并不能
# 阻止新模型发起工具调用(1/4 仍跑偏), 真正的开关是 --safe-mode。
EXTRACT_CLI_SAFE = os.environ.get("EXTRACT_CLI_SAFE", "1") not in ("0", "false", "no")

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
【食品安全类不收录(2026-11-02 收紧红线)】本系统只监测**动植物检验检疫**的政策变化。
下列属**食品安全 / 农业投入品 / 消费端**主管口径, 不是动植物检疫, 一律
is_policy_change=no:
- 农药与兽药**残留限量**(最大残留限量 / MRL / 残留标准)、农药登记与再评估、
  兽药注册与休药期;
- **饲料与食品添加剂**:饲料添加剂、饲料原料、宠物食品、营养强化剂、食品添加剂、
  加工助剂、补充剂;
- **食品安全本身**:食品安全标准、危害分析、消费端召回与警示、标签营养声称;
- **农业投入品**:肥料、土壤调理剂(除非直接构成检疫措施)。
判据是这条措施的**主管对象**是不是动植物检疫(检疫证、准入条件、有害生物与
疫病、口岸查验、证书与标识); 主语是「食品/饲料/残留/添加剂」的, 即使同时涉及
进口准入也不收录 —— 那是海关总署食品监管或农业农村部农资监管口线的政策。
例外: 进口动物/植物产品的**检疫要求本身**仍收录(如修订输华动物产品检疫证书
要求、暂停某疫区农产品输华) —— 判据是「检疫要求」而非「食品标准」。
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
现在只输出 key=value 行。
【输出格式硬约束】第一行必须直接是 record_type=policy, 不得写任何前言、解释、标题、
思考过程或代码块; 按上方字段清单顺序每行一个 key=value, 写满 27 行即止。"""


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
# 变更动词: 说明"动了规则"的动词性信号。
# 注意别把**名词**放进来 —— 2026-10-27 生产审计踩过: 「准入」本是名词, 放进动词表后
# 「标志巴西对华牛肉**准入通道**正式启用」被当成变更动作, 三条"部长见证输华牛肉启运"
# 全部漏放。登记/注册/准入/要求同理, 都在 POLICY_OBJECTS 里。
CHANGE_VERBS = (
    "禁止", "禁运", "暂停", "取消", "恢复", "放宽", "加严", "修订", "修正", "修改",
    "发布", "实施", "生效", "废止", "新增", "纳入", "允许", "解除", "加设",
    "提高", "下调", "上调", "限制", "调整", "收紧", "简化", "开放", "签发", "强制",
    "试行", "延长", "延期", "征求意见", "磋商期", "征询", "公开征求", "授权", "明确",
    # 检疫执法处置(对具体批次的检疫决定): 不是规则变化, 但属于海关/检疫机关的
    # 实际处置, 对业务有价值 —— 保留, 不当新闻垃圾清掉。
    "截获", "熏蒸", "退回", "销毁", "拦截", "召回", "没收", "处置",
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


def _page_text(raw_bytes, is_html):
    """存档字节 → 压缩空白后的可见文本(html 先剥 head/script/style/注释/标签)。"""
    text = raw_bytes.decode("utf-8", errors="replace")
    if is_html:
        # 先剥整个 <head>(内联 CSS/JS/meta 常占前 8KB+, 10-28 事故根因),
        # 再剥正文中散布的脚本/样式/注释, 最后去标签。
        text = re.sub(r"<head\b.*?</head\s*>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text,
                      flags=re.S | re.I)
        text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
        text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)


def _body_window(text, title):
    """从可见文本里取正文窗口(≤SNIP_BYTES*2 字符)。

    短页全文进 prompt; 长页优先锚定标题(文章 H1 处下刀), 锚不中则跳过页首
    导航区固定取窗。10-28 教训: 旧版只读文件头 8KB, 大 head 页面
    (argentina.gob.ar/gov.kz/ippc.int 等)模型只看到标题+样式表残片,
    12 条存档全被「无正文」拒收, 真政策(阿根廷新城疫分区化措施)被误杀。
    """
    if len(text) <= SNIP_BYTES * 2:
        return text
    anchor = re.sub(r"\s+", " ", str(title or "")).strip()[:40]
    if anchor:
        p = text.find(anchor)
        if p >= 0:
            return text[max(0, p - 100):max(0, p - 100) + SNIP_BYTES * 2]
    return text[400:400 + SNIP_BYTES * 2]


def load_archives(date_str):
    """[(meta_path, meta_dict, 正文文本), ...] —— 正文只收 .txt/.html(可解码)。

    读**整个文件**(上限 FILE_READ_BYTES)而不是文件头: 政府站 <head> 普遍
    数 KB 起步, 只读头部会让模型看不到正文(2026-10-28 全拒事故根因)。
    """
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
                        raw = f.read(FILE_READ_BYTES)
                    body = _body_window(_page_text(raw, ext == ".html"),
                                        meta.get("title") or "")
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


# ---- 食品安全类确定性闸门(2026-11-02) ----
# 用户拍板: 本系统只监测**动植物检验检疫**的政策变化, 食品安全类不收录。
# 为什么要有脚本侧闸门而不只靠 prompt: 抽取是 LLM 单轮调用, 同一份 prompt 里
# 既列了"兽药与违禁物质"主题又要求排除食品类, 模型有概率两边的规则都"沾"一点
# 就放行 —— 2026-11 生产实测正是这类通报成批进日报(ePing 面的农药/残留/MRL
# 是高产项)。规则只写在 prompt 里 = 靠模型自律; 这里加确定性判定兜住。
#
# 口径: 命中食品类**特征信号**且**没有**检疫主线信号 → 判非检疫, 拒收。
#
# 两边都命中时**放行**: 「农药残留限量的检疫批次处置」「某疫区农产品暂停输华」
# 这类仍属检疫主线, 不应误杀。误杀真政策比放过一条食安记录代价更高
# (与本模块既有取向一致: 宁可留一条待核验项, 不丢真政策)。
def screen_food_scope(rec):
    """食品安全类筛查 —— 返回 (是否拒收, 原因)。

    判定逻辑的**单一来源是 scripts/scope_guard.py** —— 与 normalize.py 入库端
    闸门共用同一套信号表。刻意不在本文件另抄一份: 抽取面与入库面两套口径必然
    漂移(这正是本仓库反复吃过的亏), 一处改了另一处没跟上就会漏放。
    """
    is_food, why, _hit = scope_guard.classify_record(rec)
    return (True, why) if is_food else (False, "")


def screen_non_policy(rec):
    """非政策内容筛查 —— 返回 (是否拒收, 原因)。五道闸门, 从严到宽。

    ① 模型自判 is_policy_change=no → 拒收(模型自己说这不是政策变化);
    ② 模型自述"整段没有措施/只有页面元数据" → 拒收(整体性否定最可信);
    ③ 食品安全类: 命中食安信号且无检疫主线信号 → 拒收(2026-11-02 新增);
    ④ 标题优先: 标题含礼宾/活动信号且标题本身无规范动作 → 拒收;
    ⑤ 关键词兜底: 命中"非政策信号"且**全文没有**"规范动作" → 拒收。
       规范动作 = 变更动词 + 政策对象 **同时**出现(或命中文号形态)。
    """
    flag = _norm_flag(rec.get("is_policy_change"))
    if flag == "no":
        note = str(rec.get("non_policy_note") or "").strip()
        return True, "模型判定非政策内容%s" % (("(%s)" % note[:40]) if note else "")

    # ③ 食品安全类闸门(2026-11-02): 在标题优先与关键词兜底之前 ——
    # 食品类通报往往有很强的"变更动词 + 政策对象"结构(修订/发布/标准/准入),
    # 会顺利穿过第⑤道; 这类必须由专门的食安闸门拦住。
    reject, why = screen_food_scope(rec)
    if reject:
        return True, why

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

    # ③ 标题优先(2026-10-27 生产审计新增): 标题是政策的一句话概括, 最有判定力。
    #    标题里出现礼宾/活动类信号(出席/仪式/表彰/启运/会议…)而标题本身**没有**规范
    #    动作时, 直接判非政策 —— 不管摘要里提到什么。
    #    (真实翻车案例: 「巴西东北部牛肉首次输华启运…唯一获中国**准入**的SIF企业…」
    #    摘要与标题都提到准入通道, 只看全文会漏放; 而「准入」是名词不是动词。)
    title = " ".join(str(x) for x in (rec.get("title_cn"), rec.get("title_en")) if x)
    if title:
        title_non = [s for s in NON_POLICY_SIGNALS if s in title]
        if title_non and not _has_normative_action(title):
            return True, "标题是非政策内容(%s)且标题无规范动作" % "、".join(title_non[:3])

    # ④ 全文兜底: 命中"非政策信号"且**全文没有**"规范动作"才判非政策
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


def call_model_direct(prompt, max_tokens=None):
    """直连 Anthropic 兼容端点(默认通道), 返回 (rc, 输出文本, usage)。

    为什么这是默认(2026-11-18 生产复盘): 见文件头 EXTRACT_* 配置块的复盘。
    一句话: 抽取是**数据面**, 不该是 Agent 面 —— 直连没有工具、没有 CLI 启动、
    没有仓库上下文注入, 也就没有"跑偏成子 Agent"这件事。

    max_tokens: 调用方可覆盖(M3.1-Flash 输出冗长 + 思考 token 吃预算, 截断的
    补救手段之一就是"失败重试时加预算重问")。

    rc: 0=成功 / 1=调用或网络失败 / 2=通道未配置(缺 key) / 124=超时。
    """
    key = (os.environ.get(EXTRACT_API_KEY_ENV) or "").strip()
    if not key:
        return 2, "未配置 %s(直连通道不可用)" % EXTRACT_API_KEY_ENV, None
    url = EXTRACT_API_BASE + "/v1/messages"
    body = json.dumps({
        "model": EXTRACT_MODEL,
        "max_tokens": int(max_tokens or EXTRACT_MAX_TOKENS),
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=EXTRACT_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        # 429 会走到这里, 调用方按 "429" 字样识别 → 提前收尾(与旧语义一致)
        return 1, "HTTP %s: %s" % (exc.code, detail), None
    except Exception as exc:
        return 1, "%s: %s" % (type(exc).__name__, str(exc)[:180]), None
    try:
        d = json.loads(raw)
    except ValueError:
        return 1, "响应不是 JSON: %s" % raw[:160], None
    text = "".join(b.get("text") or "" for b in (d.get("content") or [])
                   if isinstance(b, dict) and b.get("type") == "text")
    usage = d.get("usage") or {}
    if not text.strip():
        # 空输出几乎总是"输出被 max_tokens 截断"(M3.1-Flash 实测 2/6 如此)
        return 1, "空输出(可能被 max_tokens=%d 截断)" % EXTRACT_MAX_TOKENS, usage
    return 0, text, usage


def _cli_command(prompt):
    """拼 CLI 兜底通道的命令行: 钉死模型 + 默认 --safe-mode。"""
    tpl = os.environ.get("HERMES_RUN_CMD") or 'hermes --yolo -z "{PROMPT}"'
    if "{PROMPT}" not in tpl:
        return None, "HERMES_RUN_CMD 缺 {PROMPT} 占位符"
    # 模型钉死: 抽取不吃 ~/.hermes/config.yaml 的全局默认 —— 2026-11-16 全局模型
    # 一换, 64 份抽取的耗时立刻从 7 分钟涨到 36 分 45 秒, 而仓库里毫无记录。
    if " -m " not in tpl:
        parts = tpl.split(None, 1)
        tpl = parts[0] + " -m " + EXTRACT_MODEL + ((" " + parts[1]) if len(parts) > 1 else "")
    if EXTRACT_CLI_SAFE and "--safe-mode" not in tpl:
        parts = tpl.split(None, 1)
        tpl = parts[0] + " --safe-mode" + ((" " + parts[1]) if len(parts) > 1 else "")
    return tpl.replace("{PROMPT}", prompt.replace('"', "」")), None


def call_model_cli(prompt):
    """CLI 兜底通道, 返回 (rc, 输出文本, usage)。"""
    cmd, err = _cli_command(prompt)
    if cmd is None:
        return 2, err, None
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           errors="replace", timeout=CALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        return 124, "超时", None
    except OSError as exc:
        return 1, str(exc), None
    return p.returncode, (p.stdout or "") + (p.stderr or ""), None


_USAGE = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
_USAGE_LOCK = threading.Lock()


def _record_usage(usage):
    """把本次调用的 usage 累加进模块级计数(直连通道才有)。"""
    if not usage:
        return
    with _USAGE_LOCK:
        _USAGE["calls"] += 1
        _USAGE["input_tokens"] += int(usage.get("input_tokens") or 0)
        _USAGE["output_tokens"] += int(usage.get("output_tokens") or 0)


def backend_name():
    """本轮实际用的通道: api / cli。auto = 有 key 走 api, 否则回落 cli。"""
    if EXTRACT_BACKEND in ("api", "cli"):
        return EXTRACT_BACKEND
    return "api" if (os.environ.get(EXTRACT_API_KEY_ENV) or "").strip() else "cli"


def call_model(prompt, max_tokens=None):
    """派发到具体通道, 返回 (rc, 输出文本) —— 两元组契约保持不变(旧测试/旧调用方依赖)。

    2026-11-18: 内部换成"直连 API 优先, CLI 兜底", 但**对外仍是 (rc, out)**,
    usage 走模块级计数落台账, 不塞进返回值里。
    max_tokens: 仅直连通道支持覆盖(CLI 兜底忽略, 模板里已无该参数)。
    """
    if backend_name() == "api":
        rc, out, usage = call_model_direct(prompt, max_tokens=max_tokens)
        if rc == 2:
            # 直连没配 key → 回落 CLI(宁可慢, 不能断链)
            rc2, out2, _ = call_model_cli(prompt)
            return rc2, out2
        _record_usage(usage)
        return rc, out
    return call_model_cli(prompt)[:2]


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


def _url_host(u):
    """url 主机名(小写, 去 www. 前缀); 解析不了返回空串。"""
    try:
        host = urlparse(u).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _url_alnum(u):
    """url 归一化形态: 只留小写字母数字 —— 抹掉 / ? = . : - 等转录噪声。"""
    return re.sub(r"[^a-z0-9]", "", u.lower())


def _url_rescuable(model_url, archive_url):
    """模型回显的 url 是否可判定为「同一文档的转录变形」。

    判据: **同主机** + 归一化后一方是另一方前缀。命中覆盖四类形态:
    截断(尾巴没了) / 漏杠(路径少个分隔) / 大小写漂移 / 扩展名差异。
    判定不了(异主机、路径中途分叉如把文号改写成别的形态) → False,
    调用方维持拒收 —— 反幻觉校验不放松。
    """
    hm, ha = _url_host(model_url), _url_host(archive_url)
    if not hm or hm != ha:
        return False
    nm, na = _url_alnum(model_url), _url_alnum(archive_url)
    return bool(nm) and bool(na) and (na.startswith(nm) or nm.startswith(na))


def validate(rec, url):
    """最小校验: 标题/URL/quote 三硬项 + quote 长度。返回错误或 None。"""
    src = rec.get("source") or {}
    title = rec.get("title_cn") or rec.get("title_en")
    if not title:
        return "缺标题"
    if not (src.get("url") or rec.get("url")):
        return "缺 url"
    if src.get("url") and url and src["url"].rstrip("/") != url.rstrip("/"):
        if _url_rescuable(src["url"], url):
            # 模型回显长 URL 的**转录变形**(截断/漏杠/大小写/扩展名差异): 存档 url
            # 才是溯源权威(collect 实际抓的那份), 模型只是转述者 —— 以存档为准
            # 采纳, 不判失败。2026-11-05 实测: P0-1 无附件通报 THA/807/Add.1 的
            # 官方文档直链(docs.wto.org/...NTHA807A1.docx)两次重试都死在
            # 「url 与存档不符」上 —— 模型读对了存档, 只是转述变形, 整条报废。
            src["url"] = url
        elif src["url"] not in url and url not in src["url"]:
            # 异主机/路径中途分叉仍拒收: 反幻觉校验保留(旧口径的包含容错不变)。
            return "url 与存档不符(%s)" % src["url"][:60]
    quote = src.get("quote") or ""
    if quote and len(quote) > 120:
        src["quote"] = quote[:120]  # 截断而非拒收(保守可入库)
    if not quote:
        src["quote"] = (rec.get("title_cn") or "")[:100] or "(标题代quote)"
    rec.setdefault("record_type", "policy")
    return None


def _pct(values, q):
    """百分位(就地排序副本)。values 为空返回 0.0。"""
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, int(len(s) * q))
    return s[idx]


def _write_usage(raw_dir, date_str, backend, model, st, times):
    """把抽取用量落到 extract-usage.json(累加, 多次跑批不丢账)。

    为什么必须自己记(2026-11-18): 直连通道不写 hermes 的 state.db, 而
    token_budget.py 是从 state.db 汇总当日用量的 —— 不记这一笔, 预算闸门
    会对抽取段完全失明(而抽取正是当日用量的 95%)。
    """
    path = os.path.join(raw_dir, "extract-usage.json")
    prev = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
    try:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                old = json.load(f)
            for k in prev:
                prev[k] = int(old.get(k) or 0)
    except (OSError, ValueError):
        pass
    row = {
        "date": date_str,
        "backend": backend,
        "model": model,
        "calls": prev["calls"] + int(st.get("calls") or 0),
        "input_tokens": prev["input_tokens"] + int(st.get("input_tokens") or 0),
        "output_tokens": prev["output_tokens"] + int(st.get("output_tokens") or 0),
        "p50_sec": round(_pct(times, 0.5), 2),
        "p90_sec": round(_pct(times, 0.9), 2),
        "max_sec": round(max(times) if times else 0.0, 2),
        "updated_at": biz_now(),
    }
    try:
        os.makedirs(raw_dir, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(row, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass
    return row


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
    backend = backend_name()
    workers = max(1, EXTRACT_WORKERS)
    n_ok = n_fail = n_reject = n_skip = 0
    # 模块级 usage 计数归零: 它只反映**本轮**调用(跨轮累计由台账文件负责)
    with _USAGE_LOCK:
        for k in _USAGE:
            _USAGE[k] = 0
    log("[extract_records] %s: 待抽取 %d 份 (通道=%s 模型=%s 并发=%d)"
        % (date_str, len(archives), backend, EXTRACT_MODEL, workers))

    stop = threading.Event()
    times = []

    def worker(idx, mp, meta, url, body):
        """worker 只做"调用 + 解析 + 校验"; 闸门与落盘留给主线程(单线程写文件)。

        返回 (idx, url, rec, status, err, elapsed); status ∈ ok/skip/quota/fail。
        """
        if stop.is_set():
            return idx, url, None, "skip", "前序遇 429, 本份留待下轮", 0.0
        prompt = build_prompt(meta, url, body, today)
        last = "未知"
        t0 = time.time()
        for attempt in (1, 2):
            # M3.1-Flash 的头号失败模式是"输出被截断"(思考 token + 冗长输出把预算烧光,
            # 极端情况一个 key=value 都没吐出来)。失败重试时**加预算重问**, 而不是
            # 原样再问一遍 —— 后者只会再截断一次。
            # 第一次保持原调用签名(老测试的 mock 只接受一个参数), 第二次才带预算覆盖。
            if attempt == 1:
                rc, out = call_model(prompt)
            else:
                rc, out = call_model(prompt, max_tokens=EXTRACT_MAX_TOKENS * 2)
            if rc == 0:
                cand = parse_kv_output(out)
                err = validate(cand, url)
                if err is None:
                    return idx, url, cand, "ok", None, time.time() - t0
                last = "输出不合规格: %s" % err
            else:
                last = "调用失败 rc=%s: %s" % (rc, _clean(out)[:120])
                if "429" in out or "Token Plan" in out:
                    stop.set()
                    return idx, url, None, "quota", "Token Plan 耗尽(429)", time.time() - t0
        return idx, url, None, "fail", last, time.time() - t0

    # 并发跑: 64 份互相独立, 串行是纯浪费(实测 6 并发 4.8× 加速)。
    # 结果按**完成顺序**处理(不是存档顺序) —— 换来的是"崩了也不丢已完成的部分":
    # 每份一完成就落盘, 中途被杀不会把整轮成果带走。落盘顺序不影响台账语义
    # (同一 event_id 的记录按"空值不覆盖"合并, 与顺序无关)。
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(worker, i, *a) for i, a in enumerate(archives)]
        for fut in as_completed(futs):
            try:
                idx, url, rec, status, err, el = fut.result()
            except Exception as exc:  # worker 崩了不许带走整轮
                idx, url, rec, status, err, el = -1, "?", None, "fail", "worker 异常: %s" % exc, 0.0
            times.append(el)
            if status == "ok":
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
                try:
                    with open(out_path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                except OSError as exc:
                    n_fail += 1
                    log("[extract_records][WARN] 落盘失败 %s: %s" % (url[:60], exc))
                    continue
                n_ok += 1
                log("[extract_records] ok: %s" % (rec.get("title_cn") or url)[:70])
            elif status == "skip":
                n_skip += 1
                log("[extract_records][WARN] 跳过: %s (%s)" % (url[:60], err))
            elif status == "quota":
                # Token Plan 耗尽: 本份记失败, 剩余文档不烧重试(留待下轮)
                # 注意: 这里的 429 是 **MiniMax 账号的 Token Plan 套餐上限**
                # (报错原文: "已达到 Token Plan 用量上限: 请升级 Token Plan 套餐或购买
                # 积分补充用量 (2056)"), 不是我们的内部预算闸门 —— token_budget.py
                # 此刻可能还显示"预算内"。两码事, 别混。
                n_fail += 1
                _log_line(raw_dir, "| 抽取 | 中断 | Token Plan 耗尽(429) |")
                log("[extract_records][WARN] Token Plan 耗尽(账号套餐上限, 非内部预算), "
                    "剩余 %d 份留待下轮"
                    % (len(archives) - n_ok - n_fail - n_skip))
            else:
                n_fail += 1
                _log_line(raw_dir, "| 抽取 | 失败 | %s %s |" % (url[:70], _clean(err or "")[:60]))
                log("[extract_records][WARN] 失败: %s (%s)" % (url[:70], _clean(err or "")[:80]))

    if n_reject:
        log("[extract_records] 本轮拒收非政策内容 %d 份(已写 rejected-policy.jsonl)" % n_reject)
    if n_skip:
        log("[extract_records][WARN] %d 份因 429 留待下轮" % n_skip)
    row = _write_usage(raw_dir, date_str, backend, EXTRACT_MODEL, _USAGE, times)
    log("[extract_records] 用量: 调用 %d 次 输入 %d / 输出 %d token | 单份 p50=%.1fs p90=%.1fs max=%.1fs"
        % (row["calls"], row["input_tokens"], row["output_tokens"],
           row["p50_sec"], row["p90_sec"], row["max_sec"]))
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
