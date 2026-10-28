#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""政策变化记录标准化入库。

本分支生产数据合同只有 record_type=policy。字段权威定义见
 docs/event-schema.md; 本脚本负责校验字段、生成稳定 event_id、写 JSON 与 SQLite。
event_id 已存在时默认合并更新: 空值与兜底默认值不覆盖已有核验/研判结论,
需要整行替换时显式传 --replace。
新记录带旧闻拦截: 发布时间(source.publish_date, 缺省 event_date)早于
 --max-age-days 窗口(默认 7 天, POLICY_MAX_AGE_DAYS 覆盖, 0 关闭)判为旧闻拒收;
 已入库 event_id 的合并更新不受影响, 防止常年挂网的旧公告被当"今日新增"入日报。
聚合/目录/历史归档页拒收: source.url 命中已知列表页模式(vse-soobshcheniya /
 all-notifications / archive / news-list / event-list / notification-list 等)时,
 视为目录页而非单条政策来源, 直接拒收; 须点入目录页内具体条目原 URL 后再抽取入库。
 详见 check_source_domain() 与 prompts/policy-extraction.md 页面类型判别段。
日期口径: 一律用本模块的 biz_today()/biz_now()(东八区), 不用宿主机 date.today()
 —— 生产 VPS 系统时区是 Etc/UTC, 直接取 UTC 日期会让北京时间早间跑批整体错一天。
"""
import argparse
import datetime
import hashlib
import json
import os
import sqlite3
import urllib.parse
from datetime import date as _date

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("EPIDEMIC_DATA_DIR") or os.path.join(REPO, "data")
DB_PATH = os.environ.get("EPIDEMIC_DB") or os.path.join(REPO, "database", "epidemic.db")
VERIF_STATUS = {"verified", "single_source", "unverified", "false_positive", "merged"}
POLICY_ACTIONS = {"收紧", "放松", "调整", "恢复"}
POLICY_DOMAINS = {"animal", "plant", "both", "trade", "measures"}
POLICY_IMPACT_TYPES = {"约束", "机会", "中性"}
POLICY_IMPACT_LEVELS = {"高影响", "中影响", "低影响"}
POLICY_CHINA_RELEVANCE = {"直接涉及中国", "间接影响", "暂无明显关联"}
# 合并更新时的兜底默认值: 不覆盖库中更明确的取值, 防止重抽取抹掉核验/研判结论
# 合并更新时不得覆盖已有结论的兜底默认值(须与 fill_defaults 填的兜底保持一致)。
# 2026-10-24 补全: 原先只有 verification_status/policy_status 两键, 而
# fill_defaults 还会给 action_type/policy_domain 填兜底 —— 二轮重抽没识别出
# 动作/领域时(校验对缺省放行), "调整"/"measures" 会把库里的 "收紧"/"animal"
# 等真实结论洗掉, 直接违反"兜底默认值不覆盖"合同(实测复现)。
DEFAULT_SENTINELS = {
    "verification_status": "unverified",
    "policy_status": "已生效",
    "action_type": "调整",
    "policy_domain": "measures",
}
# 旧闻拦截默认窗口(天): 新记录发布时间早于"今天-N 天"拒收; 0 或 --max-age-days 0 关闭
DEFAULT_MAX_AGE_DAYS = 7

# ---- 业务时区(东八区) ----
# 生产 VPS 系统时区是 Etc/UTC, 而 cron 按 Asia/Shanghai 触发。若各脚本直接用
# date.today(), 北京时间早上 07:30 跑批时 UTC 仍是前一天, 日报文件名 / 旧闻窗口
# / first_seen 会整体错一天(2026-10 实测: 10-15 扫描产出 2026-10-14-* 报告)。
# 统一走这里取业务日期, 不依赖宿主机 TZ。
TZ_OFFSET_HOURS = 8


def biz_today():
    """业务日期(东八区) YYYY-MM-DD。日报命名、旧闻窗口、归档目录都以此为准。"""
    return (datetime.datetime.utcnow()
            + datetime.timedelta(hours=TZ_OFFSET_HOURS)).date().isoformat()


def biz_now():
    """业务时间(东八区) ISO8601, 用于 first_seen / updated_at 等时间戳。"""
    return (datetime.datetime.utcnow()
            + datetime.timedelta(hours=TZ_OFFSET_HOURS)).isoformat(timespec="seconds")

# ---- 官方来源域名后缀(可溯源硬要求的落地口径) ----
# source.tier=1 只应由下列官方域名的原文支撑; 第三方站点(媒体/行业站/他国贸易门户/
# 商业数据库)即使被检索命中也只能作线索, 不得标 tier 1 或据此判 verified。
# 2026-10 实测 19 条入库记录 100% unverified, 其中逾半数来源是
# freyrsolutions.com / qpests.insect-genome.com / thebrief.com.na / www.evs.ee /
# bangladeshtradeportal.gov.bd / agrinfo.eu 这类非官方站点, 却被标成 tier 1。
OFFICIAL_DOMAIN_SUFFIXES = (
    # 国际组织与通报平台
    "wto.org", "woah.org", "fao.org", "ippc.int", "eppo.int",
    # ePing 预警平台(WTO 官方, 独立域非 wto.org 子域)。
    # 2026-10 实测发现: sources.yaml 里写的是 eping.wto.org(属 wto.org 子域, 可放行),
    # 但平台实际主域已为 epingalert.org —— 白名单漏收导致本项目第一高产源的
    # tier=1 记录会被 check_source_domain 拒收(报"不在官方域名库内")。
    "epingalert.org",
    # 欧盟(EUR-Lex 为欧盟官方公报库; SANTE/DG SANTE 为措施发布主体)
    "europa.eu",
    # 欧亚经济委员会/联盟(2026-10 按样例补; 与欧盟同构的区域统一立法主体)
    # regulation.eaeunion.org 为 EEC 官方法规库; eecommission.org 为委员会官网。
    # 该主体常由成员国代为向 WTO 通报(文号前缀 KGZ/ARM 等), 措施仍出自 EEC。
    "eaeunion.org", "eecommission.org",
    # ---- 中国官方 WTO/TBT-SPS 通报平台(2026-10-15 放宽, 2026-10-18 改: 仅留省级 TBT + 商务部) ----
    # 2026-10-15 放宽是把它当"中文权威转述", 但 tbtsps.cn(海关总署标法中心)同时大量转载
    # 中国海关总署公告与商务部贸易环境专刊——AGENTS.md 明确"中国自身措施不入台账",
    # 把它当 tier 1 信源会让"中国自身措施"绕道入政策台账。2026-10-18 决定撤除 tbtsps.cn
    # (同时进 THIRD_PARTY_HINTS), 留 5 个省级/商务部站作为 tier 1: 它们转载以 WTO 通报
    # 译文为主, "中国自身措施"占比小, 信源相对纯。
    # 注意分级: 仅收录可核实的主办单位; .com/.org 商业聚合站一律不放行。
    "gdtbt.org.cn",     # 广东省 WTO/TBT 通报咨询研究中心
    "hntbt.org.cn",     # 湖南省 技术性贸易措施信息网
    "gxzl.org.cn",      # 广西壮族自治区标准技术研究院 东盟 WTO/TBT 专栏
    "wzisps.org.cn",    # 温州市标准化科学研究院
    "chinawto.mofcom.gov.cn",  # 商务部 WTO/FTA 咨询网
    # 注: tbtsps.cn(海关总署标法中心)2026-10-18 撤除, 同时进 THIRD_PARTY_HINTS 黑名单
    # —— 原因见本文件同段"中国官方 WTO/TBT-SPS 通报平台"上方注释
    # 省级政府/海关转载 WTO 通报的专栏(精确主机, 不用 .gov.cn 通配)。
    # 为什么不放行整个 gov.cn: AGENTS.md 约定中国侧措施仅作研判背景、不入政策台账,
    # 通配放行会让任何中国官方页面都能支撑 tier:1, 边界失控。
    # 这里只列**确实转载 WTO/SPS 通报原文**的通报类专栏主机(与 cn-sps-aggregator 同性质)。
    # 注意: _is_official_domain 不支持通配符, 必须逐主机精确列出。
    "amr.hainan.gov.cn",   # 海南省市场监督管理局(转载 SPS 通报中文译文)
    "gdfs.customs.gov.cn", # 海关总署(含各直属海关技贸措施风险信息提示)
    "commerce.ah.gov.cn",  # 安徽省商务厅(技贸措施通报转载)
    "commerce.shandong.gov.cn",  # 山东省商务厅(技贸措施通报转载)
    "swj.gd.gov.cn",       # 广东省商务厅
    "boc.dl.gov.cn",       # 大连市商务局(贸易环境专刊)
    # 北美
    "aphis.usda.gov", "usda.gov", "govinfo.gov", "canada.ca",
    # 英澳新(英国已脱欧, 有自有体系; 澳新为主要对华出口方)
    "gov.uk", "agriculture.gov.au",
    # 新西兰通用 .govt.nz 后缀(覆盖 mpi/beehive/afs 之外的兄弟站, 如 biosecurity.govt.nz)
    "govt.nz", "mpi.govt.nz", "beehive.govt.nz", "afs.govt.nz",
    # 亚洲
    "maff.go.jp", "mafra.go.kr", "mard.gov.vn", "dld.go.th", "doa.go.th",
    "dvs.gov.my", "pertanian.go.id", "gov.la", "gov.kz", "gov.mn",
    "bpi.da.gov.ph", "bai.gov.ph", "dahd.nic.in", "ppqs.gov.in",
    # 南美(注: 阿根廷/墨西哥官方门户主域是 gob.ar / gob.mx, 不是 gov.ar / gov.mx)
    "senasa.gov.ar", "sag.gob.cl", "gov.br", "mgap.gub.uy",
    "gob.ar", "gob.mx",
    # 欧亚 / 邻国 / 新增对华出口大户(2026-10 扩 sources.yaml 后补齐)
    "gov.ru", "gov.pl", "gov.ua", "gov.rs", "gov.mm", "gov.az",
    "gov.co", "gov.za", "gov.kp",
    # 特例 host(非标准政府域后缀, 但确实是该国官方门户)
    "tarimorman.gov.tr", "mewa.gov.sa", "nzda.govt.nz", "food.ec.europa.eu",
    "fli.de", "mattilsynet.no", "ansvsa.ro",
    "rshn-khv-eao.ru",  # 俄罗斯远东区域署, 主域是 .ru 但非 .gov.ru
    # 国际组织/其他官方登记系统
    "faolex.fao.org", "ecolex.org", "sanitary.gov",
    # ---- 次国家/区域官方门户(2026-10 依生产库 162 条实跑数据补齐) ----
    # 这批域在现网记录里真实命中且确属官方, 但不在 sources.yaml 的 track=policy
    # 源清单内(州级政府/单列口岸/特区政府), 检索层通用检索式会直接命中, 故必须放行。
    # 注意: 以下每条都对应生产库里真实出现过的 host, 不是预防性铺开。
    "gov",            # 美国受限顶级域: 一并覆盖联邦(aphis.usda.gov / federalregister.gov /
                      # whitehouse.gov / govinfo.gov)与州级(news.maryland.gov /
                      # www.tahc.texas.gov 等), 无需逐州罗列; 不会误伤 gov.bd / gob.mx 等
    "gov.hk",         # 中国香港特区政府新闻公报(info.gov.hk)
    "cfs.gov.hk",     # 香港食安中心
    "gov.sg",         # 新加坡政府(含食品局 sfa.gov.sg)
    "da.gov.ph",      # 菲律宾农业部
    "elperuano.pe",   # 秘鲁官方公报 El Peruano(Editora Perú 国企), 含检索入口 busquedas.*
    "unbs.go.ug",     # 乌干达国家标准局(东非 DEAS 通报参与方)
    "rada.gov.ua",    # 乌克兰议会官方法令库(закон.rada.gov.ua)
)

# 明显非官方的第三方站点(黑名单, 命中即警告; 命中黑名单的 tier 1 强制降为 3)
# 命中本身或其任意子域即判第三方。注意: 孟加拉只精确列 tradeportal 子域,
# 未来引入孟加拉政府官方源(dae.gov.bd / lsd.gov.bd)时不应被这条误伤。
THIRD_PARTY_HINTS = (
    "bangladeshtradeportal.gov.bd",  # 孟加拉贸易门户(转述他国措施)
    "insect-genome.com",             # 第三方检疫数据库(qpests.*)
    "thebrief.com", "thebrief.com.na",  # 纳米比亚商业通讯
    "agrinfo.eu",                    # 私营农业信息站
    "freyrsolutions.com",
    "evs.ee",
    # 2026-10 依生产库实跑数据补充: 均为"长得像官方但非官方"的域。
    # eurlexa.com 尤其危险——它是 EUR-Lex 的商业镜像, 自称 "EU legislation at
    # your fingertips", 真身是 eur-lex.europa.eu。生产库里曾用它支撑一条
    # "verified + tier 1" 的欧盟条例记录, 属可溯源硬要求的直接违规。
    "eurlexa.com",
    "c1f.com",           # 商业 .com 中文 SPS/TBT 通报聚合站, 同类见 SKILL.md:138(tbtguide.com);
                         # 不可作官方源, 命中即降 tier 3, 不可判 verified
    "tbtsps.cn",         # 2026-10-18 撤出 OFFICIAL: 信源同质化(转载中文官方材料回流自身),
                         # 且转载中国自身措施混入风险; 进黑名单双重保险, 命中即降 tier 3
    "economy.com.na",    # 私营 Namibia Economist 报纸 / IPPR 经济指南
    "base.spinform.ru",  # СПИНФОРМ 私营法律信息数据库(非俄政府门户)
    "quora.com", "wikipedia.org", "medium.com", "blogspot.com",
    "wordpress.com", "linkedin.com", "facebook.com", "twitter.com",
    "youtube.com",
)


def _source_host(url):
    """从 URL 取主机名(小写); 无法解析时返回空串。"""
    if not isinstance(url, str) or "//" not in url:
        return ""
    try:
        host = url.split("//", 1)[1].split("/", 1)[0]
    except IndexError:
        return ""
    return host.split("@")[-1].split(":")[0].strip().lower()


def _is_official_domain(host):
    """主机是否落在官方域名后缀内(含子域)。"""
    if not host:
        return False
    return any(host == s or host.endswith("." + s) for s in OFFICIAL_DOMAIN_SUFFIXES)


def _is_third_party(host):
    """主机是否命中第三方站点黑名单(按后缀匹配, 覆盖其子域)。"""
    if not host:
        return False
    return any(host == h or host.endswith("." + h) for h in THIRD_PARTY_HINTS)


# ---- 已知聚合/目录/历史归档页 URL 模式(2026-10-19 引入) ----
# 背景: 检索撞到 vse-soobshcheniya(vse-=所有)这类"全部通知"目录页时, 抽取把页面内
# 最旧条目日期当 event_date, 而 7 天窗口若被 source.publish_date 取自"今天/最近条目"绕过,
# 就会把整页历史当作新政策入库。目录页本身**没有页面级发布日期**, 不可作为单条政策的来源。
# 这些 URL 模式命中即拒收: 必须点入目录页内具体条目原 URL 后, 用条目原 URL 与原页面
# 发布日再入库。配套修改见 prompts/policy-extraction.md / skills/global-policy-search/SKILL.md。
# 注意: 路径片段都是 lower-case 匹配(URL 解析后小写化路径); query/fragment 不参与。
# 防止误伤: 仅收录**确实**是"目录/列表/归档"语义的片段, 不收录常见业务子路径。
LIST_URL_PATH_FRAGMENTS = (
    # 俄文 "vse-soobshcheniya" = "所有通知"; fsvps 整站通知汇总页
    "vse-soobshcheniya", "vse_soobshcheniya", "all-soobshcheniya", "all_soobshcheniya",
    # 英文/葡文/西文 "全部通知/新闻" 常见目录片段
    "all-notifications", "all_notifications", "all-news", "all_news",
    "all-press-releases", "all_press_releases",
    # 历史归档/索引/列表
    "/archive", "/archives", "/news/archive", "/press/archive",
    "/news-list", "/newslist", "/news/list", "/news/index",
    "/press-list", "/presslist", "/press/list",
    "/media-list", "/medialist", "/media/list",
    # 列表/索引语义(单词边界, 避免误伤业务子路径)
    # 注意: 带 query 的列表页(index.php?list= / notices?list=all)在下面
    # LIST_URL_QUERY_PARAMS 单独判定 —— 本表的片段只匹配 path, query 不参与。
    # WOAH/FAO 等机构常见的"通报列表/事件列表"页
    "/event-list", "/eventlist", "/event/list",
    "/notification-list", "/notificationlist", "/notification/list",
    "/report-list", "/reportlist", "/report/list",
    # 法文/西班牙文 "全部新闻/全部通知"
    "/toutes-les-actualites", "/todas-las-noticias", "/alle-meldungen",
    # ---- 2026-10-19 二次复盘补: 俄式"信息汇总型"长 slug 目录页 ----
    # 复现 2026-10-19 fsvps /informacija-o-vvedenii-i-snjatii-vremennyh-ogranichenij-na-postavki-
    # rastitelnoj-produkcii/ —— 翻译为"关于植物产品临时供应限制的引入与解除的信息",
    # 整页本质是"按国家 × 商品长期汇总的临时限制清单", 没有页面级发布日期。
    # 这种页 URL 用描述性长 slug(俄语 / 西语 / 法语 / 德语), 不带通用 list/archive 字样,
    # 单独建一张主题型目录页前缀表; 新增站发现同类长描述型目录页时在此追加。
    "informacija-o-", "informaciya-o-",        # 俄 "关于...的信息"
    "svodka-", "svodki-",                       # 俄 "摘要/汇总"
    "obzor-",                                   # 俄 "概览"
    "vvedenie-i-snjatie", "vvedenii-i-snjatii",  # 俄 "引入与解除" 复合前缀
    "vremennye-ogranichenija", "vremennye-ogranicheniya",  # 俄 "临时限制"
    # 西语 / 法语 "información sobre" 之类
    "informacion-sobre-", "informations-sur-",
    # 德语 "Informationen über"
    "informationen-ueber-", "informationen-uber-",
)

# ---- 目录页的 query 型信号(2026-10-19 补) ----
# 上一版把 "/index.php?list" / "/?list=" 写进 LIST_URL_PATH_FRAGMENTS, 但
# _is_aggregate_url 会先剥掉 query 再匹配路径 —— 这两条规则永远匹配不上, 属死规则。
# 现改为: 剥 query 后单独判 query 参数名。
# 命中即视为目录页: 这些参数名是 CMS/门户的"列表/栏目"入口约定
# (index.php?list=5 / notices?list=all&page=2 / ?catid=37 ...)。
# 取名偏紧: "cat" / "category" / "section" 等通用词在正文页 URL 上也常见,
# 误伤代价大于漏放, 故不收。
LIST_URL_QUERY_PARAMS = (
    "list", "list[]", "catid", "rubric",
)

# ---- 已知"目录型"主机(2026-10-19 引入) ----
# 这些主机的 `list_mode: aggregate` 在 sources.yaml 已标, 但 agent 可能从站内链接或站内
# 检索跳到同主机的**另一条**长描述型目录页(不在 LIST_URL_PATH_FRAGMENTS 里), 仍会把
# 整页历史当新政策入库。这里加 host 级兜底: 目录主机上, 凡 URL 路径**不像"深文章"**
# (没有 /press/ /news/ /p/ /article/ 等深文章段, 也没有数字 ID 段)的一律视为目录拒收。
# 复现案例 2026-10-19 fsvps /informacija-o-vvedenii-i-.../rastitelnoj-produkcii/。
# 新发现同类型目录主机时在此追加。
AGGREGATE_HOSTS = frozenset({
    # Rosselkhoznadzor 联邦兽医与植物检疫监督局 —— 整站以"信息页"形式组织临时限制清单
    "fsvps.gov.ru", "www.fsvps.gov.ru",
})

# 路径中含这些片段之一 → 视为"深文章"(具体公告/单条政策页), 不被 host 兜底拦
# 用单复数都收, 避免站点 URL 风格不一致
_DEEP_ARTICLE_MARKERS = (
    "/press-service/", "/press/", "/press-release", "/pressreleases/",
    "/news/", "/news-", "/new/",                  # 站内新闻条目
    "/article/", "/articles/", "/p/", "/post/",   # CMS 风格
    "/publication/", "/publications/",
    "/detail/", "/view/", "/item/",
    "/document/", "/doc/",                        # 文档
    "/notice/", "/notices/",                      # 通报
    "/announcement/", "/announcements/",
    "/regulation/", "/resolution/", "/order/",    # 法规/决议/命令
    "/act/", "/acts/",                            # 法律行为
)

# 路径段含 3+ 位连续数字(典型站内 ID) → 视为具体条目页; 短数字(<3)可能是版本号, 不收
_ARTICLE_ID_RE = __import__("re").compile(r"/\d{3,}(?:/|$)")


def _looks_like_article_path(path):
    """在 host 兜底里判断: URL 路径是否看起来像一篇具体文章/单条政策条目。"""
    if any(m in path for m in _DEEP_ARTICLE_MARKERS):
        return True
    if _ARTICLE_ID_RE.search(path):
        return True
    return False


def _is_aggregate_url(url):
    """URL 是否命中已知聚合/目录/历史归档页模式(必须点入条目原 URL 抽取)。

    列表页/历史归档页/全站通知汇总页/长描述型信息页等**目录型** URL, 自身没有页面
    级发布日期, 把页面内某个条目的日期当作 event_date 是误读; 入库端必须拒收。

    三层判断:
    1) 路径片段黑名单(LIST_URL_PATH_FRAGMENTS): 通用 list/archive/vse- 等;
    1b) query 参数黑名单(LIST_URL_QUERY_PARAMS): index.php?list= / notices?catid= 等
        列表型入口 —— query 已被剥出 path, 必须在 query 上单独判;
    2) 已知目录主机(AGGREGATE_HOSTS) + URL 路径不像"深文章" → 一律视为目录
       (复现 fsvps 站内任意"informacija-o-*"长描述型信息页, 这些不在通用黑名单里);
       主机按**后缀**匹配, 同站子域(mcx.fsvps.gov.ru 等)同样适用;
    3) 返回 True / False。空 URL / 非字符串 / 无路径段 → False。
    """
    if not isinstance(url, str) or "//" not in url:
        return False
    # 拆 URL: scheme://host/path?query#fragment
    try:
        after_scheme = url.split("//", 1)[1]  # "host/path..."
    except IndexError:
        return False
    # 取 path 段(去掉 query 和 fragment)
    path_and_query = after_scheme.split("/", 1)[1] if "/" in after_scheme else ""
    path, _, query = path_and_query.partition("?")
    path = path.split("#", 1)[0]
    query = query.split("#", 1)[0]
    if not path and not query:
        return False
    # 标准化: 小写, 补前导 /, 与 LIST_URL_PATH_FRAGMENTS 的 "/xxx" 形式对齐
    path = "/" + path if path and not path.startswith("/") else path
    path = path.lower()

    # 第 1 层: 通用黑名单(路径片段)
    if path and any(frag in path for frag in LIST_URL_PATH_FRAGMENTS):
        return True

    # 第 1b 层: 列表型 query 参数(路径匹配不到时, query 才是信号)
    if query:
        try:
            params = urllib.parse.parse_qs(query, keep_blank_values=True)
        except ValueError:
            params = {}
        if any(k.lower() in LIST_URL_QUERY_PARAMS for k in params):
            return True

    # 第 2 层: host 级兜底(已知目录主机 + 路径不像深文章 → 视为目录)
    host = _source_host(url)
    if _host_matches(host, AGGREGATE_HOSTS) and not _looks_like_article_path(path):
        return True

    return False


def _host_matches(host, suffixes):
    return any(host == s or host.endswith("." + s) for s in suffixes)


def check_source_domain(e):
    """校验 source 与 tier 是否自洽。

    返回 (errors, normalized_event):
    - tier=1 但主机非官方域 -> 报错拒收(除非该国确无已知官方域, 用 source.official=1 显式声明)
    - 命中第三方黑名单 -> tier 强制降为 3, 该条**不入库**(只可作 search-log 线索),
      且不得判 verified
    - 命中已知聚合/目录页模式 -> 拒收, 不可作为单条政策来源
    """
    errs = []
    src = e.get("source") or {}
    url = src.get("url")
    host = _source_host(url)
    tier = src.get("tier")

    # 主机取不到(缺 scheme / 相对路径 / 畸形 URL)时, 下面两处判定都会被 host 真值短路:
    # 第三方黑名单查不到、官方域校验也查不到 —— 结果是 `tbtsps.cn/x.html` 这种
    # **无 scheme** 的拉黑源能以 tier=1 + verified 直接入库(2026-10-22 审计实测复现)。
    # 抽取时 URL 被截断是常见失误, 校验必须对此 fail-closed 而不是放行。
    if not host:
        errs.append(
            "source.url 无法解析出主机名(%r): 必须带 scheme 的绝对 URL, 如 https://host/path; "
            "缺 scheme 会同时绕过第三方黑名单与官方域校验, 一律拒收" % url)
        e["source"] = src
        return errs, e

    # 聚合/目录/历史归档页: 自身没有页面级发布日期, 把页面内条目日期当 event_date
    # 是误读。整页不可作为单条政策的来源, 须点入页面内具体条目原 URL 后再抽取入库。
    # 2026-10-19 引入: 复现 fsvps /vse-soobshcheniya 把 2026-07-31 历史条目误读为新政策
    if _is_aggregate_url(url):
        errs.append(
            "source.url 命中已知聚合/目录/历史归档页模式(%s); 目录页本身没有页面级发布日期, "
            "不可作为单条政策来源。请点入该目录页内具体条目原 URL 后, 用条目原 URL 与原页面"
            "发布日再入库; 抽取阶段详见 prompts/policy-extraction.md 页面类型判别段" % url)
        return errs, e

    if host and _is_third_party(host):
        # 第三方站点: 只能是线索, 不进政策台账(降 tier 3 + 本条拒收)。
        # 注意: 这里 append 的都会让 validate 失败 —— 即整条不入库, 而不是"降级后入库"。
        if tier == 1:
            src["tier"] = 3
        if e.get("verification_status") in ("verified",):
            errs.append("来源为第三方站点(%s), 不得判 verified(仅可线索)" % host)
        errs.append("来源主机 %s 命中第三方站点黑名单: 本条不入库(仅可写进 search-log.md 当线索); "
                    "须回溯该国官方原文后重新入库(tier 已降为 3, 不得判 verified)" % host)
        e["source"] = src
        return errs, e

    if tier == 1 and host and not _is_official_domain(host):
        # 逃生口只认真正的"是"。原先用真值判断, 结果 official: "0" / "false" /
        # "no" / "off" 这类**字符串**全是真值, 等于随手写一个就能打开逃生口。
        # 语义应当 fail-closed: 只有 1 / true / yes / 是 才算数, 其余(含非法值)
        # 一律按"没声明"处理并报错。
        flag = src.get("official")
        truthy = (flag is True
                  or (isinstance(flag, int) and not isinstance(flag, bool) and flag == 1)
                  or (isinstance(flag, str)
                      and flag.strip().lower() in ("1", "true", "yes", "y", "是")))
        if not truthy:
            errs.append(
                "source.tier=1 但来源主机 %s 不在官方域名库内; "
                "若该国确有本系统未收录的官方域, 在 source 里显式加 official: 1 说明" % host)
        else:
            # official:1 逃生口: 允许 tier=1, 但必须**留痕** —— 该条 tier=1 不是靠
            # 官方域名库支撑, 而是靠抽取环节的显式声明, 事后可审计、可回查。
            # (2026-10-19 修正: 原来这里是 e.setdefault("verification_notes", None),
            #  既没留痕, 又白占一个本该由核验环节写的字段。)
            note = ("tier=1 依据 source.official=1 显式声明放行(主机 %s 不在 OFFICIAL_DOMAIN_SUFFIXES "
                    "内), 非域名库支撑, 请人工复核该 URL 确为该国官方站点" % host)
            src["official_note"] = note
            e["source"] = src
    return errs, e


def now():
    return biz_now()


def _subject_components(e):
    """计算 event_id 的 subject 关联对象: policy_key 或四个关联字段之一非空。"""
    products = e.get("products") or []
    targets = e.get("target_countries") or []
    return [
        (e.get("policy_key") or "").strip(),
        (e.get("disease_name_en") or "").strip().lower(),
        ",".join(sorted(str(x).strip().lower() for x in products if str(x).strip())),
        ",".join(sorted(str(x).strip().lower() for x in targets if str(x).strip())),
        (e.get("scope") or "").strip().lower(),
    ]


def event_id_of(e):
    products = e.get("products") or []
    targets = e.get("target_countries") or []
    subject = e.get("policy_key") or "|".join([
        str(e.get("disease_name_en") or "").strip().lower(),
        ",".join(sorted(str(x).strip().lower() for x in products)),
        ",".join(sorted(str(x).strip().lower() for x in targets)),
        str(e.get("scope") or "").strip().lower(),
    ])
    key = "|".join([
        "policy", str(e.get("country_en", "")).strip().lower(),
        str(e.get("policy_domain") or "").strip().lower(),
        str(e.get("action_type") or "").strip().lower(), subject,
        str(e.get("effective_date") or e.get("event_date") or "").strip(),
    ])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _parse_date(value):
    """把日期串解析为真实的日历日期; 不是合法日期返回 None(不猜)。

    接受 YYYY / YYYY-MM / YYYY-MM-DD, 以及 ISO 日期时间前缀
    (2026-10-19T00:00:00, 取日期部分)。语义非法一律 None:
    2026-13-45 / 2026-02-30 / 2026-00-01 / TBD 都不是日期。
    """
    s = str(value or "").strip()
    if not s:
        return None
    if len(s) > 10:                      # ISO 日期时间: 取日期部分
        s = s[:10]
    try:
        if len(s) == 4:
            return datetime.date(int(s), 1, 1)
        if len(s) == 7:
            return datetime.date(int(s[:4]), int(s[5:7]), 1)
        if len(s) == 10:
            return datetime.date.fromisoformat(s)
    except ValueError:
        return None
    return None


def _norm_date(value):
    """把 YYYY / YYYY-MM 粒度补齐为 YYYY-MM-DD; 不是合法日历日期返回 None(不猜)。

    2026-10-19 修正(A3 闭环): 原实现只按**长度**判断, "2026-13-45" 这类
    语义非法的 10 位串会被原样返回, 于是旧闻窗口的字符串比较把它当成
    "比窗口下限还新"而放行 —— 脏日期反而能绕过拦截入库。
    """
    d = _parse_date(value)
    return d.isoformat() if d else None


def published_date(e):
    """发布时间口径: 优先 source.publish_date(页面发布日), 缺省退回 event_date/effective_date。

    页面发布日是"这条情报何时出现"的真实信号; event_date 是公告/生效日期,
    今日新发布的公告完全可能引用更早的生效日, 不能用 event_date 反推旧闻。
    """
    src = e.get("source") if isinstance(e.get("source"), dict) else {}
    for cand in (src.get("publish_date"), e.get("event_date"), e.get("effective_date")):
        d = _norm_date(cand)
        if d:
            return d
    return None


def stale_reason(e, cutoff):
    """时间窗校验: 发布时间早于 cutoff(YYYY-MM-DD)判为旧闻, 返回拒收原因; 其余返回 None。

    2026-10-19 修正(A3 闭环): 日期字段**解析不了**时按"日期不明"拒收(fail closed),
    不放行。技能红线写明"页面完全没有日期线索时...按旧闻处理"; 入库端已拦掉非法
    event_date/effective_date, 这里的分支兜住手工构造/存量脏数据。
    """
    if not cutoff:
        return None
    d = published_date(e)
    if d is None:
        return "日期不明(发布时间/事件日期均不是合法日期, 无法判定新旧)"
    if d < cutoff:
        return "发布时间 %s 早于窗口下限 %s" % (d, cutoff)
    return None


def _event_exists(con, event_id):
    return con.execute(
        "SELECT 1 FROM events WHERE event_id = ?", (event_id,)).fetchone() is not None


def validate(e):
    errs = []
    if e.get("record_type") not in (None, "policy"):
        errs.append("record_type 必须为 policy")
    for key in ("country_cn", "country_en", "source"):
        if not e.get(key):
            errs.append("缺少必填字段 %s" % key)
    if not (e.get("event_date") or e.get("effective_date")):
        errs.append("政策记录需要 event_date 或 effective_date")
    # 日期必须是真实的日历日期(2026-10-19 A3 闭环): 2026-13-45 / 2026-02-30 /
    # "TBD" 这类脏值若放行, 一来旧闻窗口的字符串比较会把它当成"比窗口还新"从而
    # 绕过拦截, 二来日报表格会原样端出无法辨认的日期。源头拒收, 不给下游留雷。
    for key in ("event_date", "effective_date"):
        val = e.get(key)
        if val and _parse_date(val) is None:
            errs.append("%s 不是合法日期: %r(应为 YYYY / YYYY-MM / YYYY-MM-DD 的真实日历日期)"
                        % (key, val))
    if e.get("category") not in (None, "policy"):
        errs.append("政策记录的 category 必须为 policy")
    if e.get("action_type") and e["action_type"] not in POLICY_ACTIONS:
        errs.append("action_type 必须为 收紧|放松|调整|恢复")
    if e.get("policy_domain") and e["policy_domain"] not in POLICY_DOMAINS:
        errs.append("policy_domain 取值不合法")
    if not (e.get("title_cn") or e.get("title_en") or e.get("summary_cn")):
        errs.append("政策记录需要 title_cn / title_en / summary_cn 至少其一")
    # 关联对象必填: event_id 强依赖 subject, 否则同国/同领域/同动作/同日的两条政策
    # 会算出同一 event_id 并被默认合并吞掉。policy_key 是显式兜底。
    if not any(_subject_components(e)):
        errs.append(
            "event_id 关联对象必填: 至少给出 policy_key / disease_name_en / "
            "products / target_countries / scope 之一(原文未提及时给显式 policy_key 兜底)"
        )
    src = e.get("source")
    if not isinstance(src, dict) or not src.get("url"):
        errs.append("source.url 必填(可溯源是硬要求)")
    if isinstance(src, dict):
        if not src.get("quote"):
            errs.append("source.quote 必填")
        elif len(str(src["quote"])) > 120:
            errs.append("source.quote 不得超过 120 字")
    if e.get("verification_status") and e["verification_status"] not in VERIF_STATUS:
        errs.append("verification_status 非法: %s" % e["verification_status"])
    # 来源域名与 tier 自洽性: 第三方站点不得标 tier 1 / 不得判 verified
    dom_errs, _ = check_source_domain(e)
    errs.extend(dom_errs)
    if e.get("impact_type") and e["impact_type"] not in POLICY_IMPACT_TYPES:
        errs.append("impact_type 必须为 约束|机会|中性")
    if e.get("impact_level") and e["impact_level"] not in POLICY_IMPACT_LEVELS:
        errs.append("impact_level 必须为 高影响|中影响|低影响")
    if e.get("china_relevance") and e["china_relevance"] not in POLICY_CHINA_RELEVANCE:
        errs.append("china_relevance 取值不合法")
    return errs


def fill_defaults(e):
    e["record_type"] = "policy"
    e["category"] = "policy"
    e["event_date"] = e.get("effective_date") or e.get("event_date")
    d = str(e["event_date"]).strip()
    precision = e.get("date_precision")
    if len(d) == 4:
        d, precision = d + "-01-01", precision or "year"
    elif len(d) == 7:
        d, precision = d + "-01", precision or "month"
    else:
        precision = precision or "day"
    e["event_date"], e["date_precision"] = d, precision
    # products / target_countries: 合同是**数组**; P4 键值路径给的是逗号分隔字符串。
    # 字符串直接进 join/sorted 会逐字符拆(2026-10-28 实测: 日报商品列渲染成
    # p、o、u、l、t、r、y…; event_id 的 subject 在无 policy_key 时也随字符序失稳),
    # 入库端统一归一成数组, 各消费方(report/docx/webapp/risk)不再各自兜底。
    for key in ("products", "target_countries"):
        v = e.get(key)
        if isinstance(v, str):
            e[key] = [p.strip() for p in
                      v.replace("，", ",").replace("；", ";").replace(";", ",").split(",")
                      if p.strip()]
        elif v is None or v == "":
            e[key] = []
        elif not isinstance(v, list):
            e[key] = [str(v)]
    e.setdefault("policy_domain", "measures")
    e.setdefault("action_type", "调整")
    e.setdefault("policy_status", "已生效")
    e.setdefault("effective_date", e["event_date"])
    e.setdefault("impact_type", None)
    e.setdefault("impact_level", None)
    e.setdefault("impact_score", None)
    e.setdefault("impact_focus", None)
    e.setdefault("impact_rationale", None)
    e.setdefault("china_relevance", None)
    e.setdefault("recommended_action", None)
    # dimension_scores 必须是 dict: 显式 null(Agent "无法评估" 时会写 null)若原样保留,
    # 后续 risk.py 读维度会 TypeError。setdefault 不覆盖显式 null, 故这里显式归一。
    if not isinstance(e.get("dimension_scores"), dict):
        e["dimension_scores"] = {}
    e.setdefault("disease_name_cn", None)
    e.setdefault("disease_name_en", None)
    e.setdefault("region", None)
    e.setdefault("cross_sources", [])
    e.setdefault("checked_urls", [])
    e.setdefault("verification_status", "unverified")
    e.setdefault("summary_cn", None)
    ts = now()
    e["event_id"] = e.get("event_id") or event_id_of(e)
    e["first_seen"] = e.get("first_seen") or ts
    e["updated_at"] = ts
    return e


def open_db(path=None):
    path = path or DB_PATH
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE IF NOT EXISTS events ("
        " event_id TEXT PRIMARY KEY, payload TEXT NOT NULL,"
        " disease_en TEXT, country_en TEXT, category TEXT, event_date TEXT,"
        " verification_status TEXT, impact_level TEXT, impact_focus TEXT, updated_at TEXT)"
    )
    cols = {r[1] for r in con.execute("PRAGMA table_info(events)").fetchall()}
    if "record_type" not in cols:
        con.execute("ALTER TABLE events ADD COLUMN record_type TEXT")
    if "impact_level" not in cols:
        con.execute("ALTER TABLE events ADD COLUMN impact_level TEXT")
    if "impact_focus" not in cols:
        con.execute("ALTER TABLE events ADD COLUMN impact_focus TEXT")
    con.execute("CREATE INDEX IF NOT EXISTS idx_events_date ON events(event_date)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_events_status ON events(verification_status)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(record_type)")
    return con


def upsert(con, e, merge=True):
    row = con.execute("SELECT payload FROM events WHERE event_id = ?", (e["event_id"],)).fetchone()
    if row and merge:
        old = json.loads(row[0])
        patch = {k: v for k, v in e.items()
                 if v not in (None, [], {}) and k not in ("event_id", "first_seen")}
        for key, default in DEFAULT_SENTINELS.items():
            if patch.get(key) == default and old.get(key) not in (None, "", default):
                del patch[key]
        e = dict(old, **patch)
        e["event_id"] = old.get("event_id") or e.get("event_id")
        e["first_seen"] = old.get("first_seen") or e.get("first_seen")
        e["updated_at"] = now()
    con.execute(
        "INSERT OR REPLACE INTO events "
        "(event_id,payload,disease_en,country_en,category,event_date,verification_status,impact_level,impact_focus,updated_at,record_type) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (e["event_id"], json.dumps(e, ensure_ascii=False), None,
         e.get("country_en"), "policy", e.get("event_date"),
         e.get("verification_status"), e.get("impact_level"), e.get("impact_focus"), e.get("updated_at"), "policy"),
    )
    con.commit()
    return e


def load_events(con, where="", args=()):
    sql = "SELECT payload FROM events" + (" WHERE " + where if where else "")
    return [json.loads(r[0]) for r in con.execute(sql, args).fetchall()]


def write_event_file(e, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "%s.json" % e["event_id"])
    with open(path, "w", encoding="utf-8") as f:
        json.dump(e, f, ensure_ascii=False, indent=2)
    return path


def _load_input(spec):
    raw = __import__("sys").stdin.read() if spec == "-" else open(spec, encoding="utf-8").read()
    if spec.endswith(".jsonl"):
        # JSONL(每行一条, 2026-10-24 引入): 取数段改为逐条 append 一行, 消灭
        # "python 读改写整个 JSON" 的上下文放大(实测每追加一条就把已增长的
        # 整个文件读回一次, 是取数段单轮消耗 65k→200k 的主因之一)。
        # 坏行/空行跳过并计数 —— 断在半条的最后一行不该堵死整批。
        records, bad = [], 0
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                bad += 1
        if bad:
            print("[WARN] JSONL 输入跳过 %d 条解析失败的行(常见于写入中断的半行)" % bad)
    else:
        data = json.loads(raw)
        records = data.get("events") or [data] if isinstance(data, dict) else data
    if not isinstance(records, list):
        raise ValueError("输入的 events 必须是数组(当前是 %s)" % type(records).__name__)
    return records


def count_first_seen(date_str, db_path=None):
    """统计某个业务日期当天入库(first_seen)的政策条数。

    first_seen 存在 payload JSON 里而不是独立列(见 upsert/load_events), 所以这里
    逐行解析而不是写 SQL LIKE —— 免得依赖 JSON 序列化格式。
    2026-10-22 新增: run_scan.sh 的产物校验需要它来区分"安静的一天"与
    "取到了东西却没入库"(后者是 10-22 同款假成功, 必须判失败)。
    """
    con = open_db(db_path)
    if con is None:
        return 0
    try:
        rows = con.execute("SELECT payload FROM events").fetchall()
    except sqlite3.Error:
        return 0
    finally:
        con.close()
    n = 0
    for (payload,) in rows:
        try:
            d = json.loads(payload) if isinstance(payload, str) else (payload or {})
        except ValueError:
            continue
        if str(d.get("first_seen") or "")[:10] == date_str:
            n += 1
    return n


def main():
    ap = argparse.ArgumentParser(description="政策变化记录标准化入库")
    ap.add_argument("--input", help="政策 JSON 文件路径; - 为 stdin")
    ap.add_argument("--count-first-seen", metavar="YYYY-MM-DD",
                    help="只查询: 打印该业务日期当天入库(first_seen)的政策条数后退出")
    ap.add_argument("--out", help="政策 JSON 落盘目录")
    ap.add_argument("--db", action="store_true", help="写入 SQLite")
    ap.add_argument("--db-path", help="SQLite 路径覆盖")
    ap.add_argument("--update", action="store_true", help="(已废弃) 合并更新已是默认行为")
    ap.add_argument("--replace", action="store_true",
                    help="整行替换已有政策(默认按 event_id 合并, 空值/默认状态不覆盖已核验、已研判字段)")
    ap.add_argument("--max-age-days", type=int, default=None, metavar="N",
                    help="旧闻拦截: 新记录发布时间(source.publish_date, 缺省 event_date)"
                         "距今天超过 N 天拒收; 默认取环境变量 POLICY_MAX_AGE_DAYS, 未设为 %d;"
                         " 0 关闭(历史回补/库重建重放时用)" % DEFAULT_MAX_AGE_DAYS)
    args = ap.parse_args()
    if args.count_first_seen:
        print(count_first_seen(args.count_first_seen, args.db_path))
        return 0
    if not args.input:
        print("[错误] 需要 --input <文件> (或用 --count-first-seen <日期> 做只读查询)")
        return 2
    try:
        records = _load_input(args.input)
    except (OSError, ValueError) as exc:
        print("[错误] 读取/解析输入失败: %s" % exc)
        return 2
    events, failed = [], []
    for i, e in enumerate(records):
        errs = validate(e)
        if errs:
            failed.append((i, errs))
            continue
        # 发布日期写坏不等于不可入库(它可以退回 event_date 判新旧), 但必须提醒
        # 抽取环节: publish_date 应当是页面自己的发布日, 取不到就填 null,
        # 写 "TBD"/"2026-13-45" 这种值等于把判定依据交给了 event_date。
        src = e.get("source")
        pub = src.get("publish_date") if isinstance(src, dict) else None
        if pub and _parse_date(pub) is None:
            print("[WARN] 第%d条 source.publish_date 不是合法日期(%r), 旧闻窗口将退回 event_date 判定; "
                  "取不到请填 null" % (i, pub))
        events.append((i, fill_defaults(dict(e))))
    for i, errs in failed:
        print("[校验失败] 第%d条: %s" % (i, "; ".join(errs)))
    if not events:
        return 2
    max_age = args.max_age_days
    if max_age is None:
        try:
            max_age = int(os.environ.get("POLICY_MAX_AGE_DAYS", DEFAULT_MAX_AGE_DAYS))
        except ValueError:
            max_age = DEFAULT_MAX_AGE_DAYS
    cutoff = None
    if max_age > 0:
        cutoff = (_date.fromisoformat(biz_today())
                  - datetime.timedelta(days=max_age)).isoformat()
    out_dir = args.out or os.path.join(DATA_DIR, "events", biz_today())
    con = open_db(args.db_path) if (args.db or args.db_path) else None
    # 旧闻拦截只针对新增 event_id: 已入库记录的核验/研判回写走合并更新, 不受窗口限制
    stale, written = [], 0
    for i, e in events:
        reason = stale_reason(e, cutoff)
        if reason and not (con and _event_exists(con, e["event_id"])):
            stale.append((i, reason))
            print("[旧闻拒收] 第%d条: %s  %s @ %s" % (
                i, reason, e.get("title_cn") or e.get("title_en") or e.get("summary_cn") or "（无标题）",
                e.get("country_en")))
            continue
        if con:
            e = upsert(con, e, merge=not args.replace)
        write_event_file(e, out_dir)
        written += 1
        print("[OK] %s  政策  %s @ %s  %s  [%s] 影响:%s" % (
            e["event_id"], e.get("title_cn") or e.get("title_en") or e.get("summary_cn") or "（无标题）",
            e.get("country_en"), e.get("event_date"), e.get("verification_status"),
            e.get("impact_level") or "未研判"))
    print("\n入库 %d 条(校验失败 %d 条, 旧闻拒收 %d 条) -> %s%s" % (
        written, len(failed), len(stale), out_dir,
        (" + SQLite: %s" % (args.db_path or DB_PATH)) if con else ""))
    if stale:
        print("提示: 确属新线索或历史回补/库重建时, 用 --max-age-days <N> 放宽窗口, 0 为关闭校验")
    return 1 if (failed or stale) else 0


if __name__ == "__main__":
    raise SystemExit(main())
