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

判定文本分两段(2026-11-04, M22 修法):
  · **散文部分** = 标题 + 摘要 + 原文引用 —— 强信号(对象本身就是食品)必须
    在这里命中才算数;
  · **商品部分** = products 清单 + target_countries —— **降权**, 只有弱信号
    专属词在这里命中才定罪; 强信号只在商品清单里出现不足以定罪。
  为什么: 动检通报的 products 常是**整批限制清单**(活体动物/肉/乳/饲料/皮张/
  旧设备/种用材料), 任何一项食品信号都会把整条真政策拖下水 —— 实证误杀:
  哈萨克斯坦临时限制俄四州活体动物及畜产品进口(321bcf4df94f) 被整条判成
  食安, 后靠手工 --unmark 挽回, 但旧口径未修, 下次 --apply 会重新误杀。

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


# ---- 弱信号专属词(＝食品信号表减去强信号表) ----
# 为什么要分这一档(2026-11-04, M22 修法):
#   强信号(宠物食品/饲料添加剂/MRL/…)在**散文部分**(标题/摘要/原文引用)命中 → 定罪;
#   只在**商品字段**(products 清单)命中 → **降权**, 不足以定罪。
#   实证: 哈萨克斯坦临时限制俄四州活体动物及畜产品进口(321bcf4df94f) 的 products
#   是整批限制清单(活体动物/猪肉及野猪肉制品/乳制品/**饲料及饲料添加剂**/皮张/旧设备),
#   其中只有「饲料及饲料添加剂」命中强信号, 而函件正文里没有任何检疫专有词 →
#   旧口径把它整条判成食安并标了 false_positive(真政策误杀)。长尾限制清单是
#   动检通报的常态, 商品字段命中不能单独定罪 —— 强信号必须在标题/摘要里出现。
#   弱信号专属词(肥料/土壤调理/加工助剂/补充剂/强化剂/食品安全规范/农药再评估/
#   withdrawal period/fertiliser/soil conditioner)没有这种"限制清单"歧义,
#   出现在任何地方都定罪。
FOOD_WEAK_ONLY_CN = tuple(s for s in FOOD_SIGNALS_CN if s not in FOOD_STRONG_SIGNALS_CN)
FOOD_WEAK_ONLY_EN = tuple(s for s in FOOD_SIGNALS_EN if s not in FOOD_STRONG_SIGNALS_EN)

# 降权放行的原因前缀(供下游区分"没命中"与"命中但降权放行")
DOWNGRADE_PREFIX = "仅商品字段命中强信号"


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


def classify_scope(text, goods_text=""):
    """判定一段文本的收录口径。

    返回 (是否食品类非检疫口径, 原因文本, 命中信号)。
    判定文本为空 → 不判(返回 False), 让下游按缺字段处理。

    参数:
      text      —— **散文部分**: 标题 + 摘要 + 原文引用(原文原话, 信号最强)。
      goods_text —— **商品部分**: products 清单 + target_countries(降权, 见下)。

    判定顺序(2026-11-04, M22 修法):
      1. 先看**检疫主线信号**是否在场(散文或商品清单里出现都算)。在场 → 放行
         (除非第 2 步命中散文强信号)。
      2. **散文强信号**命中(对象本身就是食品: 宠物食品/饲料添加剂/MRL/兽药残留…)
         → 判拒收。但若文本同时明确以检疫措施为主语(含检疫/疫区/证书/有害生物/
         口岸查验等检疫专有词), 仍放行 —— 因为「农药残留超标的检疫退货处置」
         是检疫处置, 不是食品安全标准修订。判据是**检疫专有词**而非泛词
         (「进口」「措施」这类两边都有的词不算)。
      3. **弱信号**命中(散文或商品清单均可) → 判拒收。
      4. 只有**商品字段**命中强信号 → **降权放行**(返回 False, 原因带
         DOWNGRADE_PREFIX 前缀留痕)。理由: 动检通报的 products 常是整批限制
         清单(活体动物/肉/乳/饲料/皮张/旧设备), 任何一项食品信号都会把整条
         真政策拖下水 —— 这就是 M22 的实证误杀路径。
    """
    prose = text or ""
    goods = goods_text or ""
    if not prose.strip() and not goods.strip():
        return False, "", None

    all_text = (prose + " " + goods).strip()

    # 检疫主线信号: 只认**检疫专有词**(检疫/疫区/有害生物/证书/口岸查验…),
    # 不认「进口」「措施」这类两边都有的泛词 —— 泛词会把食安记录误判成检疫。
    quarantine = (_match_cn(all_text, QUARANTINE_SIGNALS_CN)
                  or _match_en(all_text, QUARANTINE_SIGNALS_EN))

    # 强信号只在**散文部分**命中才算数(M22 修法: 强信号须在标题/摘要命中)
    strong = (_match_cn(prose, FOOD_STRONG_SIGNALS_CN)
              or _match_en(prose, FOOD_STRONG_SIGNALS_EN))
    if strong:
        if quarantine:
            # 检疫专有词在场 → 这条讲的是检疫措施(哪怕涉及残留/饲料字样),
            # 放行。例:「农药残留超标的检疫退货处置」「宠物食品的进境检疫要求」。
            return False, "", strong
        return True, ("食品安全类非检疫口径(强信号:%s, 无检疫主线信号)" % strong), strong

    # 弱信号: 散文命中即可; 商品清单命中**弱信号专属词**也算(这类词没有
    # "限制清单"歧义, 见 FOOD_WEAK_ONLY_CN 注释)。
    weak = (_match_cn(prose, FOOD_WEAK_ONLY_CN)
            or _match_en(prose, FOOD_WEAK_ONLY_EN)
            or _match_cn(goods, FOOD_WEAK_ONLY_CN)
            or _match_en(goods, FOOD_WEAK_ONLY_EN))
    if weak:
        if quarantine:
            return False, "", weak
        return True, ("食品安全类非检疫口径(命中:%s, 无检疫主线信号)" % weak), weak

    # 只在商品字段命中强信号 → 降权放行, 但把信号带回去留痕。
    goods_strong = (_match_cn(goods, FOOD_STRONG_SIGNALS_CN)
                    or _match_en(goods, FOOD_STRONG_SIGNALS_EN))
    if goods_strong:
        return False, ("%s:%s(降权放行, 留待人工核验)" % (DOWNGRADE_PREFIX, goods_strong)), goods_strong
    return False, "", None


def record_scope_text(e):
    """从一条政策记录里取出用于口径判定的**散文部分**。

    = 标题 + 摘要 + 原文引用(引用是原文原话, 信号最强)。

    商品字段(products / target_countries)**不在这里** —— 它是降权部分, 见
    record_scope_goods_text。两者分开是为了能区分"标题里写了宠物食品"(定罪)
    与"限制清单里列了宠物食品"(不足以定罪), 见 classify_scope 第 4 步。
    """
    src = e.get("source") if isinstance(e.get("source"), dict) else {}
    parts = [
        str(e.get("title_cn") or ""), str(e.get("title_en") or ""),
        str(e.get("summary_cn") or ""), str(src.get("quote") or ""),
    ]
    return " ".join(p for p in parts if p)


def record_scope_goods_text(e):
    """从一条政策记录里取出**商品部分**(降权判定用)。

    products 清单 + target_countries。动检通报的 products 常是整批限制清单
    (活体动物/肉/乳/饲料/皮张/旧设备/种用材料), 食品词出现在其中只说明
    "被限制的货物里有一项是食品", 不能说明这条政策的主语是食品。
    """
    parts = []
    for key in ("products", "target_countries"):
        v = e.get(key)
        if isinstance(v, list):
            parts.extend(str(x) for x in v)
        elif v:
            parts.append(str(v))
    return " ".join(p for p in parts if p)


def classify_record(e):
    """判定一条政策记录(完整记录)是否属于食品类非检疫口径。

    三个接入点(extract_records 抽取闸门 / normalize 入库闸门 / flag_food_scope
    存量清理)统一走这里, 避免各自拼判定文本导致口径漂移。
    返回 (是否食品类非检疫口径, 原因文本, 命中信号)。
    """
    return classify_scope(record_scope_text(e), record_scope_goods_text(e))
