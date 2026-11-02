# -*- coding: utf-8 -*-
"""食品类(食品安全/农业投入品)口径判定 —— 动植物检疫台账的收录红线。

为什么单独成模块(2026-11-02):
  用户拍板「本系统只监测**动植物检验检疫**的政策变化, 食品类不要」。但食品类
  政策不是从一个口子进来的, 而是三条并行的口子:
    ① ePing 通报面(fetch_eping.py 的 TBT 放行白名单)—— 各国 MRL/农药/饲料
       通报是 ePing 的高产项, 批量涌进候选;
    ② 检索面(config/sources.yaml 的 policy_keywords / policy_queries)——
       词库里有"食品添加剂/饲料原料/宠物食品/MRL", 检索式直接去搜它们;
    ③ 抽取面(prompts/policy-extraction.md 的主题清单 + extract_records.py 的
       抽取闸门)—— 主题清单把"食品/饲料添加剂与原料"列为常见主题。
  三处若各写一套判定, 口径必然漂移(这正是本仓库反复吃过的亏)。所以判定逻辑
  **单一来源**放这里, 三处都 import 它。

判定口径(与 prompts/policy-extraction.md 的红线段一致):
  命中食品类信号 且 **没有**检疫主线信号 → 判为非检疫口径, 不入台账。
  两者都命中 → 放行。理由: 「农药残留限量的检疫批次处置」「某疫区农产品暂停
  输华」这类仍属检疫主线; 误杀真政策的代价远高于放过一条食安记录
  (与 extract_records.py 既有取向一致: 宁可留一条待核验项, 不丢真政策)。

信号表刻意分中英两套:
  · 中文按子串匹配(中文无词边界概念);
  · 英文按**词边界**匹配 —— 子串匹配会把 "mrl" 命中 "myrtle"、"seed" 命中
    "sesame"、"residue" 命中 "residues" 之外的普通词, 造成误杀。
"""
import re

# ---- 食品类信号(中文, 子串匹配) ----
FOOD_SIGNALS_CN = (
    # 残留限量 / 农药兽药
    "残留限量", "最大残留", "残留标准", "农药残留", "兽药残留",
    "农药登记", "农药再评估", "兽药注册", "休药期",
    # 饲料与食品添加剂
    "饲料添加剂", "饲料原料", "宠物食品", "食品添加剂", "加工助剂",
    "营养强化", "膳食补充", "补充剂", "强化剂", "营养声称", "营养标签",
    # 食品安全本身
    "食品安全标准", "危害分析", "食品安全规范",
    # 农业投入品
    "肥料", "土壤调理",
)

# ---- 食品类信号(英文, 词边界匹配) ----
FOOD_SIGNALS_EN = (
    "maximum residue limit", "maximum residue limits", "residue limit",
    "residue limits", "residue level", "residue levels",
    "pesticide registration", "pesticide residue", "pesticide residues",
    "veterinary drug", "veterinary drugs", "veterinary medicine",
    "veterinary medicines", "drug withdrawal period", "withdrawal period",
    "feed additive", "feed additives", "feedstuff", "feed stuff",
    "pet food", "pet foods", "petfood",
    "food additive", "food additives", "food safety",
    "food supplement", "food supplements", "dietary supplement",
    "dietary supplements", "nutritional claim", "fortification",
    "fertiliser", "fertilizer", "soil conditioner",
)

# ---- 检疫主线信号(中文, 子串匹配) ----
# 只认**检疫专有词**。刻意**不含**「准入」「措施」「进口」「进出口」这类两边
# 都有的泛词: 食安侧的"宠物食品进口准入条件""添加剂进口措施"会因此被误判成
# 检疫主线而获得豁免(2026-11 实测正是这个漏放路径)。检疫专有词本身已足够
# 判别检疫主线 —— 检疫/疫区/有害生物/证书/口岸查验/通关/进境/寄生物。
QUARANTINE_SIGNALS_CN = (
    "检疫", "疫区", "有害生物", "寄生物", "病媒",
    "口岸", "查验", "通关", "进境", "隔离检疫",
    "检疫证", "证书", "标识", "耳标", "疫情", "输华", "进口许可",
)

# ---- 检疫主线信号(英文, 词边界匹配) ----
QUARANTINE_SIGNALS_EN = (
    "quarantine", "phytosanitary", "zoosanitary", "sanitary certificate",
    "veterinary certificate", "phytosanitary certificate",
    "import permit", "import permits", "pest control", "infestation",
    "eradication", "epidemic", "border inspection", "pre-clearance",
    "animal health", "plant health", "regulated pest", "quarantine pest",
)

# ---- 食品类「强信号」: 即使同时命中检疫主线信号也必须拒收 ----
# 为什么要单独一档: 「准入」「进口」这类词在**动检与食安两边都常见** ——
# 「宠物食品进口准入条件」既是食品政策又含检疫主线词, 按"命中检疫信号就放行"
# 的规则会被豁免, 于是宠物食品这类以"进口准入"形式出现的食安政策照样进台账
# (2026-11 实测正是这类漏网)。故把**对象本身就是食品**的词提为强信号:
# 主语是食品/饲料/残留/添加剂, 就不因"进口准入"字样而获得检疫豁免。
#
# 强信号与普通食品信号的差别只在"能否被检疫主线信号豁免", 两者都判为拒收。
FOOD_STRONG_SIGNALS_CN = (
    "宠物食品", "饲料添加剂", "食品添加剂", "饲料原料", "残留限量",
    "最大残留", "农药残留", "兽药残留", "残留标准", "农药登记",
    "兽药注册", "休药期", "食品安全标准", "危害分析", "营养强化",
    "膳食补充", "营养声称", "营养标签",
)
FOOD_STRONG_SIGNALS_EN = (
    "maximum residue limit", "maximum residue limits", "residue limit",
    "residue limits", "residue level", "residue levels",
    "pesticide registration", "pesticide residue", "pesticide residues",
    "veterinary drug", "veterinary drugs", "veterinary medicine",
    "veterinary medicines", "drug withdrawal period",
    "feed additive", "feed additives", "feedstuff", "feed stuff",
    "pet food", "pet foods", "petfood",
    "food additive", "food additives", "food safety",
    "food supplement", "food supplements", "dietary supplement",
    "dietary supplements", "nutritional claim", "fortification",
)


def _match_cn(text, signals):
    """中文信号按子串匹配。返回命中的信号或 None。"""
    low = text.lower()
    for s in signals:
        if s.lower() in low:
            return s
    return None


def _match_en(text, signals):
    """英文信号按词边界匹配(避免 mrl→myrtle、seed→sesame 这类子串误伤)。"""
    low = text.lower()
    for s in signals:
        if re.search(r"(?<![a-z])" + re.escape(s.lower()) + r"(?![a-z])", low):
            return s
    return None


def classify_scope(text):
    """判定一段文本的收录口径。

    返回 (是否食品类非检疫口径, 原因文本, 命中信号)。
    判定文本为空 → 不判(返回 False), 让下游按缺字段处理。

    判定顺序(2026-11-02 二次校准):
      1. 先看**检疫主线信号**是否在场。在场 → 放行(除非命中"对象本身就是食品"
         的强信号且该强信号是主语 —— 见第 2 步的例外)。
      2. 强信号(对象本身就是食品: 宠物食品/饲料添加剂/MRL/兽药残留…)命中 →
         判拒收。但若文本同时**明确以检疫措施为主语**(含检疫/疫区/证书/
         有害生物/口岸查验等检疫专有词), 仍放行 —— 因为
         「农药残留超标的检疫退货处置」是检疫处置, 不是食品安全标准修订。
         判据是**检疫专有词**而非泛词(「进口」「措施」这类两边都有的词不算)。
      3. 普通食品信号命中且无检疫主线信号 → 判拒收。
    """
    if not text or not text.strip():
        return False, "", None

    # 检疫主线信号: 只认**检疫专有词**(检疫/疫区/有害生物/证书/口岸查验…),
    # 不认「进口」「措施」这类两边都有的泛词 —— 泛词会把食安记录误判成检疫。
    quarantine = (_match_cn(text, QUARANTINE_SIGNALS_CN)
                  or _match_en(text, QUARANTINE_SIGNALS_EN))

    strong = (_match_cn(text, FOOD_STRONG_SIGNALS_CN)
              or _match_en(text, FOOD_STRONG_SIGNALS_EN))
    if strong:
        if quarantine:
            # 检疫专有词在场 → 这条讲的是检疫措施(哪怕涉及残留/饲料字样),
            # 放行。例:「农药残留超标的检疫退货处置」「宠物食品的进境检疫要求」。
            return False, "", strong
        return True, ("食品安全类非检疫口径(强信号:%s, 无检疫主线信号)" % strong), strong

    food = _match_cn(text, FOOD_SIGNALS_CN) or _match_en(text, FOOD_SIGNALS_EN)
    if not food:
        return False, "", None
    if quarantine:
        return False, "", food
    return True, ("食品安全类非检疫口径(命中:%s, 无检疫主线信号)" % food), food


def record_scope_text(e):
    """从一条政策记录里取出用于口径判定的文本。

    取标题 + 摘要 + 原文引用(引用是原文原话, 信号最强) + 商品字段。
    商品字段单列: 很多食安通报的标题很泛("关于批准 XXX 的法规"), 但 products
    里写着"宠物食品""饲料添加剂", 靠商品字段才拦得住。
    """
    src = e.get("source") if isinstance(e.get("source"), dict) else {}
    parts = [
        str(e.get("title_cn") or ""), str(e.get("title_en") or ""),
        str(e.get("summary_cn") or ""), str(src.get("quote") or ""),
    ]
    for key in ("products", "target_countries"):
        v = e.get(key)
        if isinstance(v, list):
            parts.extend(str(x) for x in v)
        elif v:
            parts.append(str(v))
    return " ".join(p for p in parts if p)
