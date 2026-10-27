#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""疫见全球 · 最小回归自测(stdlib only, 无 pytest 依赖)。

覆盖两个 P1 修复后的关键合同不变量:
- templates/base.py: openpyxl 不再模块级导入,样式函数在缺包时按需抛 ImportError
- scripts/normalize.py: event_id 关联对象必填,杜绝同 country/domain/action/date 的两条政策碰撞合并
- scripts/normalize.py: 合并保留 event_id / first_seen
- scripts/normalize.py: 旧闻拦截(发布时间早于 --max-age-days 窗口的新记录拒收,
  已入库 event_id 的合并更新不受影响, 0 关闭)
- scripts/normalize.py: 目录/历史归档/全站通知汇总页拒收(2026-10-19 引入;
  source.url 命中 LIST_URL_PATH_FRAGMENTS 即拒收, 不论日期如何)
- scripts/risk.py: 四维加权合同(score / level / focus 三者一致, 0.05 容差)
- scripts/push_report.py: 评审核闸门(2026-10-19 引入; REVIEW_CHANNEL + --review-approved
  两段式, 默认行为向后兼容, --message 自定义文本不走闸门)

用法:
  python scripts/test_smoke.py            # 简洁输出
  python scripts/test_smoke.py -v         # 详细输出
退出码: 全部通过 -> 0, 任一失败 -> 1。
"""
import datetime
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# 测试环境隔离: 把 data/ / database/ 指到 tmp, 避免污染真实库
TMP = tempfile.mkdtemp(prefix="radar-smoke-")
os.environ["EPIDEMIC_DATA_DIR"] = TMP
os.environ["EPIDEMIC_DB"] = os.path.join(TMP, "smoke.db")

# 推送渠道变量同样要隔离(2026-10-22): push_scope.load_hermes_env() 会去读
# ~/.hermes/.env 补齐渠道变量, 而生产那台机器的 .env 里**真的配了钉钉**。
# 于是同一套测试在本地(无渠道)全绿、在生产恒挂 3 条 —— 自检在生产上等于失效,
# 绿灯与红灯都不代表代码状态。这里显式清干净并把 .env 路径指到不存在的文件,
# 需要真实 .env 的用例自己 mock HERMES_ENV_PATH(见 PushScopeLockTests)。
for _k in ("WECHAT_WEBHOOK", "DINGTALK_WEBHOOK", "DINGTALK_SECRET", "SMTP_HOST",
           "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "SMTP_TO", "HERMES_PUSH_CHANNELS",
           "REVIEW_CHANNEL"):
    os.environ.pop(_k, None)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "templates"))

import push_scope as _push_scope  # noqa: E402  (须在 sys.path 注入之后)
_push_scope.HERMES_ENV_PATH = os.path.join(TMP, "no-such-.hermes-env")


def _source_event(**overrides):
    """构造一条合法政策记录的最小集,单测可在它基础上改字段。"""
    base = {
        "record_type": "policy",
        "category": "policy",
        "title_cn": "测试政策",
        "title_en": "Test policy",
        "country_cn": "演示国",
        "country_en": "Demo Land",
        "policy_domain": "animal",
        "action_type": "收紧",
        "event_date": "2026-09-15",
        "effective_date": "2026-09-15",
        "products": ["新鲜牛肉"],
        "source": {
            # 默认用官方域名, 否则会命中 source tier/域名自洽性校验
            "tier": 1, "name": "测试源", "url": "https://www.aphis.usda.gov/p1",
            "publish_date": "2026-09-15", "quote": "test quote",
        },
    }
    base.update(overrides)
    return base


class _OpenpyxlBlocker:
    """插入 sys.meta_path 拦截 openpyxl.* 的导入, 直接抛 ImportError。"""

    def find_spec(self, name, path=None, target=None):
        if name == "openpyxl" or name.startswith("openpyxl."):
            raise ImportError("simulated missing openpyxl")
        return None


class BaseLazyImportTests(unittest.TestCase):
    """P1#1: templates/base.py 不再模块级 import openpyxl。"""

    def test_base_has_no_top_level_openpyxl_import(self):
        # 用 AST 只检测真正的模块级导入; 函数体内的按需导入是允许的
        import ast
        path = os.path.join(REPO, "templates", "base.py")
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=path)
        for node in tree.body:
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name == "openpyxl" or name.startswith("openpyxl."):
                    self.fail("templates/base.py 第 %d 行存在模块级 openpyxl 导入: %s"
                              % (node.lineno, name))

    def test_base_imports_when_openpyxl_blocked(self):
        blocker = _OpenpyxlBlocker()
        sys.meta_path.insert(0, blocker)
        try:
            self._purge(["base", "openpyxl"])
            import base  # noqa: F401
            self.assertIn("base", sys.modules)
        finally:
            sys.meta_path.remove(blocker)
            self._purge(["base", "openpyxl"])

    def test_style_function_raises_without_openpyxl(self):
        blocker = _OpenpyxlBlocker()
        sys.meta_path.insert(0, blocker)
        try:
            self._purge(["base", "openpyxl"])
            import base
            with self.assertRaises(ImportError):
                base.style_header_row(None, 1)
        finally:
            sys.meta_path.remove(blocker)
            self._purge(["base", "openpyxl"])

    @staticmethod
    def _purge(prefixes):
        for k in list(sys.modules):
            if any(k == p or k.startswith(p + ".") for p in prefixes):
                sys.modules.pop(k, None)


class SourceDomainTests(unittest.TestCase):
    """可溯源硬要求: tier=1 只能由官方域名支撑, 第三方站点只作线索。"""

    def setUp(self):
        import normalize
        self.n = normalize
        self.base = _source_event(
            source={"tier": 1, "name": "src",
                    "url": "https://www.aphis.usda.gov/rule/123", "quote": "q"},
            verification_status="unverified")

    def test_official_domain_tier1_passes(self):
        self.assertEqual(self.n.validate(dict(self.base)), [])

    def test_url_without_scheme_rejected(self):
        """2026-10-22 审计红线: 缺 scheme 的 URL 取不到 host, 会同时短路
        「第三方黑名单」与「tier=1 必须官方域」两处判定 —— 拉黑的 tbtsps.cn 写成
        `tbtsps.cn/x.html` 就能以 tier=1 + verified 直接入库。必须 fail-closed。"""
        for url in ("tbtsps.cn/x.html", "freyrsolutions.com/notice/1", "https://"):
            e = _source_event(
                source={"tier": 1, "name": "n", "url": url, "quote": "q"},
                verification_status="verified")
            errs = self.n.validate(dict(e))
            self.assertTrue(errs, "缺 scheme 的 URL 竟被放行: %s" % url)
            self.assertTrue(any("scheme" in m for m in errs), errs)

    def test_official_flag_only_accepts_true_values(self):
        """2026-10-22 审计: 逃生口原先是真值判断, official:"0"/"false"/"no"
        这类字符串全是真值, 等于随手写一个就能打开 official:1 逃生口。"""
        url = "https://www.example-somewhere.org/notice/1"   # 不在官方域库内
        for bad in ("0", "false", "no", "off", "", "maybe"):
            e = _source_event(
                source={"tier": 1, "name": "n", "url": url, "quote": "q",
                        "official": bad},
                verification_status="unverified")
            errs = self.n.validate(dict(e))
            self.assertTrue(errs, "official=%r 竟打开了逃生口" % bad)
        for good in (1, True, "1", "true", "yes"):
            e = _source_event(
                source={"tier": 1, "name": "n", "url": url, "quote": "q",
                        "official": good},
                verification_status="unverified")
            self.assertEqual(self.n.validate(dict(e)), [], "official=%r 应放行" % good)

    def test_third_party_host_rejected(self):
        # 2026-10-19 文案修正: 行为不变(第三方来源整条拒收不入库), 只是把错误文案
        # 改成与实现一致 —— 原文案写"[线索] ... 已将 tier 降为 3", 让人以为会入库。
        e = _source_event(
            source={"tier": 1, "name": "n",
                    "url": "https://freyrsolutions.com/notice/1", "quote": "q"},
            verification_status="unverified")
        errs = self.n.validate(dict(e))
        self.assertTrue(any("第三方" in m for m in errs), errs)
        self.assertTrue(any("不入库" in m for m in errs), errs)

    def test_third_party_tier_downgraded_to_3(self):
        e = _source_event(
            source={"tier": 1, "name": "n",
                    "url": "https://www.evs.ee/news/9", "quote": "q"},
            verification_status="unverified")
        self.n.validate(dict(e))
        self.assertEqual(e["source"]["tier"], 3)

    def test_third_party_cannot_be_verified(self):
        e = _source_event(
            source={"tier": 2, "name": "n",
                    "url": "https://qpests.insect-genome.com/x", "quote": "q"},
            verification_status="verified")
        errs = self.n.validate(dict(e))
        self.assertTrue(any("不得判 verified" in m for m in errs), errs)

    def test_unlisted_domain_rejected_unless_declared(self):
        e = _source_event(
            source={"tier": 1, "name": "n",
                    "url": "https://somedomain.gov.example/x", "quote": "q"},
            verification_status="unverified")
        errs = self.n.validate(dict(e))
        self.assertTrue(any("不在官方域名库内" in m for m in errs), errs)
        e2 = _source_event(
            source={"tier": 1, "name": "n", "official": 1,
                    "url": "https://somedomain.gov.example/x", "quote": "q"},
            verification_status="unverified")
        self.assertEqual(self.n.validate(dict(e2)), [])

    def test_known_official_hosts_recognized(self):
        for url in ("https://eur-lex.europa.eu/eli/reg/2026/2268/oj",
                    "https://eping.wto.org/en/SPS/Notifications",
                    "https://www.agriculture.gov.au/biosecurity",
                    "https://www.gov.uk/government/organisations/defra",
                    # 2026-10 扩 sources.yaml 时补入的官方域后缀, 防回归
                    "https://fsvps.gov.ru/ru/vse-soobshcheniya",
                    "https://www.wetgiw.gov.pl/",
                    "https://dpss.gov.ua/en",
                    "https://www.minpolj.gov.rs/",
                    "https://www.moa.gov.mm/",
                    "https://afsa.gov.az/",
                    "https://www.minagricultura.gov.co/paginas/default.aspx",
                    "https://www.dlrrd.gov.za/",
                    "https://www.argentina.gob.ar/senasa",
                    "https://www.gob.mx/senasica",
                    "https://www.tsis.fli.de/Reports/Info.aspx"):
            self.assertTrue(self.n._is_official_domain(self.n._source_host(url)), url)

    def test_bd_tradeportal_specifically_flagged_not_whole_gov_bd(self):
        # 2026-10 修订: 不再用 "gov.bd" 全杀孟加拉, 只精确命 tradeportal 子域,
        # 让 dae.gov.bd / lsd.gov.bd 等真实政府源不被误伤。
        self.assertTrue(self.n._is_third_party("bangladeshtradeportal.gov.bd"))
        self.assertFalse(self.n._is_third_party("dae.gov.bd"))
        self.assertFalse(self.n._is_third_party("lsd.gov.bd"))

    def test_subnational_official_hosts_from_production_db(self):
        # 2026-10 依生产库 162 条实跑数据补齐: 这些 host 曾真实入库过,
        # 且确属官方, 但都不在 sources.yaml 的 track=policy 源清单内。
        for url in ("https://www.info.gov.hk/gia/general/202610/13/P2026101300xxx.htm",
                    "https://www.cfs.gov.hk/english/whatsnew/whatsnew_fa/whatsnew_fa.html",
                    "https://www.sfa.gov.sg/newsroom/news-details",
                    "https://www.da.gov.ph/press-releases",
                    "https://news.maryland.gov/",
                    "https://www.tahc.texas.gov/",
                    "https://www.federalregister.gov/documents/2026/10/01/2026-18xxx",
                    "https://www.whitehouse.gov/presidential-actions/2026/10/",
                    "https://busquedas.elperuano.pe/dec_normas-legales/",
                    "https://webstore.unbs.go.ug/",
                    "https://zakon.rada.gov.ua/laws/show/2768#Text"):
            self.assertTrue(self.n._is_official_domain(self.n._source_host(url)), url)

    def test_us_gov_suffix_covers_federal_and_state(self):
        # "gov" 作为美国受限顶级域后缀, 应同时覆盖联邦与州级门户
        for host in ("www.aphis.usda.gov", "www.federalregister.gov",
                     "www.whitehouse.gov", "news.maryland.gov",
                     "www.tahc.texas.gov", "www.agriculture.wa.gov"):
            self.assertTrue(self.n._is_official_domain(host), host)

    def test_lookalike_non_official_hosts_flagged(self):
        # 2026-10 生产库审计发现: 三个"像官方但不是"的域曾支撑 tier 1 记录。
        # eurlexa.com 尤其关键——它是 EUR-Lex 的商业镜像(真身 eur-lex.europa.eu)。
        for host in ("eurlexa.com", "www.eurlexa.com", "economy.com.na",
                     "base.spinform.ru"):
            self.assertTrue(self.n._is_third_party(host), host)
            self.assertFalse(self.n._is_official_domain(host), host)

    def test_eurlexa_tier1_cannot_be_verified(self):
        # 复现生产库问题: 仿冒域曾支撑一条 verified + tier 1 的欧盟条例记录
        e = _source_event(
            source={"tier": 1, "name": "EUR-Lex 仿冒镜像",
                    "url": "https://eurlexa.com/act/en/32026R2268/32026R2268/text",
                    "quote": "Commission Implementing Regulation (EU) 2026/2268"},
            verification_status="verified")
        errs = self.n.validate(dict(e))
        self.assertTrue(any("不得判 verified" in m for m in errs), errs)
        self.assertEqual(e["source"]["tier"], 3)

    def test_known_third_party_hosts_flagged(self):
        for host in ("bangladeshtradeportal.gov.bd", "thebrief.com.na",
                     "agrinfo.eu", "freyrsolutions.com", "www.evs.ee"):
            self.assertTrue(self.n._is_third_party(host), host)


class AggregateUrlTests(unittest.TestCase):
    """目录/历史归档/全站通知汇总页不可作为单条政策来源(2026-10-19 引入)。

    复现案例: 2026-10-19 fsvps /vse-soobshcheniya 整页历史被误读为新政策入库。
    根因: 目录页自身没有页面级发布日期, 抽取把页面内最旧条目日期当 event_date,
    7 天窗口若被 source.publish_date 取自"今天/最新条目"绕过就放行。
    修复: normalize.py 加 LIST_URL_PATH_FRAGMENTS 黑名单, 命中即拒收;
    配合 prompt / skill / sources.yaml 三层防御。
    """

    def setUp(self):
        import normalize
        self.n = normalize

    def test_fsvps_vse_soobshcheniya_aggregate_url_flagged(self):
        # 复现 2026-10-19 案例: fsvps /vse-soobshcheniya 是"所有通知"目录页
        self.assertTrue(self.n._is_aggregate_url(
            "https://fsvps.gov.ru/ru/vse-soobshcheniya"))

    def test_known_aggregate_url_patterns_caught(self):
        for url in (
            "https://fsvps.gov.ru/ru/vse-soobshcheniya",
            "https://example.gov/news-list",
            "https://example.gov/news-list/2026",
            "https://example.gov/news/list?page=2",
            "https://example.gov/all-notifications",
            "https://example.gov/press-list",
            "https://example.gov/archive",
            "https://example.gov/archives/2026/09",
            "https://example.gov/news/archive",
            "https://example.gov/event-list",
            "https://example.gov/notification-list",
            "https://example.gov/report-list",
        ):
            self.assertTrue(self.n._is_aggregate_url(url), url)

    def test_normal_business_urls_not_flagged(self):
        # 防误伤: 普通业务子路径 / 单条政策页 / 具体公告 ID 都应放行
        for url in (
            "https://fsvps.gov.ru/ru/fsvps/press-service/news/12345",
            "https://www.aphis.usda.gov/aphis/newsroom/news/sa_by_date/sa-2026",
            "https://eur-lex.europa.eu/eli/reg/2026/2268/oj",
            "https://epingalert.org/en/SPS/Notifications/Detail/12345",
            "https://www.maff.go.jp/j/press/syouan/2026_10_01.html",
            "https://www.gov.uk/government/news/example-uk-policy-2026",
            "https://www.federalregister.gov/documents/2026/10/15/2026-12345",
            # 新闻条目页面: 单词 "list" 在查询参数里不是路径段
            "https://example.gov/news/article-2026-10-15",
        ):
            self.assertFalse(self.n._is_aggregate_url(url), url)

    def test_aggregate_url_validation_rejects_record(self):
        # 关键回归: 哪怕来源是官方域(fsvps .gov.ru), 命中聚合模式也必须拒收
        e = _source_event(
            source={"tier": 1, "name": "fsvps",
                    "url": "https://fsvps.gov.ru/ru/vse-soobshcheniya",
                    "publish_date": "2026-10-19", "quote": "目录页内容截取..."})
        errs = self.n.validate(dict(e))
        self.assertTrue(any("聚合" in m or "目录" in m for m in errs), errs)

    def test_aggregate_url_rejects_even_with_old_event_date_published_recent(self):
        # 复现 2026-10-19 绕过 7 天窗口的具体场景:
        # source.publish_date = 今天(2026-10-19, 抽错"页面访问日"),
        # event_date = 2026-07-31(目录页内最旧条目日期)。
        # 没有聚合模式校验时, 这条会被放行(因为 publish_date 在窗口内)；
        # 加上 LIST_URL_PATH_FRAGMENTS 后, 直接拒收, 不论日期如何。
        e = _source_event(
            event_date="2026-07-31", effective_date="2026-07-31",
            source={"tier": 1, "name": "fsvps",
                    "url": "https://fsvps.gov.ru/ru/vse-soobshcheniya",
                    "publish_date": "2026-10-19", "quote": "目录页内容截取..."})
        errs = self.n.validate(dict(e))
        self.assertTrue(any("聚合" in m for m in errs), errs)

    def test_fsvps_long_descriptive_aggregate_url_rejected(self):
        # 2026-10-19 二次复盘: 通用黑名单没拦住 fsvps 站另一条长描述型目录页
        # /informacija-o-vvedenii-i-snjatii-vremennyh-ogranichenij-na-postavki-rastitelnoj-produkcii/
        # 翻译: 关于植物产品临时供应限制的引入与解除的信息。本质是按国家 × 商品
        # 长期汇总的临时限制清单, 自身没有页面级发布日期。
        # 修复: host 兜底(AGGREGATE_HOSTS) + _looks_like_article_path 双重判断,
        # 该 URL 路径里没有 /press/ /news/ /article/ 等深文章标记也没有数字 ID,
        # → 视为目录拒收。
        url = ("https://fsvps.gov.ru/informacija-o-vvedenii-i-snjatii-"
               "vremennyh-ogranichenij-na-postavki-rastitelnoj-produkcii/")
        self.assertTrue(self.n._is_aggregate_url(url), url)
        e = _source_event(
            source={"tier": 1, "name": "fsvps", "url": url,
                    "publish_date": "2026-10-19",
                    "quote": "页面是按国家 × 商品长期汇总的临时限制清单"})
        errs = self.n.validate(dict(e))
        self.assertTrue(any("聚合" in m for m in errs), errs)

    def test_fsvps_article_path_not_flagged_by_host_fallback(self):
        # 防 host 兜底误伤: fsvps 站内具体文章页(有 /press-service/ /news/ + 数字 ID)
        # 应被放行, 不被 host 兜底当作目录。
        for url in (
            "https://fsvps.gov.ru/press-service/news/12345",
            "https://fsvps.gov.ru/ru/press/news/98765",
            "https://fsvps.gov.ru/news/2026/10/15/some-article-12345",
            "https://fsvps.gov.ru/article/12345",
        ):
            self.assertFalse(self.n._is_aggregate_url(url), url)

    def test_other_aggregate_path_fragments_caught(self):
        # 2026-10-19 二次复盘补充: 俄式/欧式"信息汇总型"长 slug 前缀
        for url in (
            "https://fsvps.gov.ru/svodka-2026-10/",
            "https://fsvps.gov.ru/obzor-ogranichenij/",
            "https://fsvps.gov.ru/vvedenii-i-snjatii-vremennyh-ogranichenij/",
            "https://fsvps.gov.ru/vremennye-ogranichenija-na-postavki/",
            "https://example.gov/informacion-sobre-restricciones/",
            "https://example.gov/informations-sur-restrictions/",
            "https://example.gov/informationen-ueber-beschraenkungen/",
        ):
            self.assertTrue(self.n._is_aggregate_url(url), url)

    def test_query_type_list_pages_rejected(self):
        # 2026-10-19 三次复盘: 上一版把 "/index.php?list" / "/?list=" 写进了路径黑名单,
        # 但 _is_aggregate_url 先剥 query 再匹配路径 —— 这两条规则永远匹配不上(死规则),
        # 于是 index.php?list=5 / notices?list=all&page=2 这类目录页能绕过拦截。
        for url in (
            "https://example.gov/index.php?list=5",
            "https://example.gov/notices?list=all&page=2",
            "https://example.gov/press.php?catid=37",
        ):
            self.assertTrue(self.n._is_aggregate_url(url), url)

    def test_query_check_does_not_over_block_article_urls(self):
        # 防误伤: 普通正文页即使带 query 也应放行
        for url in (
            "https://www.aphis.usda.gov/newsroom/news/sa-2026?page=2",
            "https://eur-lex.europa.eu/eli/reg/2026/2268/oj?locale=en",
            "https://fsvps.gov.ru/press-service/news/12345",
        ):
            self.assertFalse(self.n._is_aggregate_url(url), url)

    def test_aggregate_host_fallback_covers_subdomains(self):
        # 2026-10-19 三次复盘: 目录主机兜底原先是精确匹配 host, 同站子域
        # (mcx.fsvps.gov.ru 等)不在名单内, 同样形态的长描述型目录页可绕过。
        for url in (
            "https://fsvps.gov.ru/dopolnitelnye-svedeniya-2026/",
            "https://www.fsvps.gov.ru/dopolnitelnye-svedeniya-2026/",
            "https://mcx.fsvps.gov.ru/dopolnitelnye-svedeniya-2026/",
        ):
            self.assertTrue(self.n._is_aggregate_url(url), url)
        # 深文章页仍放行
        self.assertFalse(self.n._is_aggregate_url(
            "https://mcx.fsvps.gov.ru/press-service/news/12345"))

    def test_official_declaration_leaves_audit_trail(self):
        # 2026-10-19: official:1 逃生口原来既不留痕, 又白占 verification_notes
        # (本该由核验环节写的字段)。现在写进 source.official_note, 事后可审计。
        e = _source_event(
            source={"tier": 1, "name": "n", "official": 1,
                    "url": "https://somedomain.gov.example/x", "quote": "q"},
            verification_status="unverified")
        self.assertEqual(self.n.validate(dict(e)), [])
        self.assertIn("official_note", e["source"])
        self.assertIn("somedomain.gov.example", e["source"]["official_note"])
        # 核验环节的 verification_notes 不得被占用
        self.assertIsNone(e.get("verification_notes"))


class PushReportReviewGateTests(unittest.TestCase):
    """评审核闸门(2026-10-19 引入): 用户在微信/钉钉端提测试请求时, 反馈必须在原渠道完成;
    评审核闸门强制: REVIEW_CHANNEL=wechat 模式下, 首跑只发微信 + 写 .review-pending,
    --review-approved 才把剩余渠道发完并清理待办。

    复现 2026-10-19: 用户强调"先微信审核", 但 push_report.py 旧版一发就发齐
    (WECHAT_WEBHOOK + DINGTALK_WEBHOOK 同时配就同时发), 不分阶段。
    """

    def setUp(self):
        import push_report
        import push_scope
        self.pr = push_report
        self.ps = push_scope
        self.tmp = tempfile.mkdtemp(prefix="push-gate-")
        # push_report 在 import 时已经从 normalize 锁定 DATA_DIR, 模块级常量
        # PUSH_FLAG_DIR / PENDING_DIR 也已基于旧 DATA_DIR 解析。这里把它们
        # 重新指向当前测试 tmp 目录, 让每个测试用独立的 flag/pending 状态。
        self.flag_dir = os.path.join(self.tmp, "reports", ".pushed")
        self.pending_dir = os.path.join(self.tmp, "reports", ".review-pending")
        # 范围锁目录同样要隔离: 2026-10-20 引入推送范围锁后, 锁不隔离会让
        # 上一条用例的"只发企业微信"漏进下一条用例, 用例之间互相污染。
        self.scope_dir = os.path.join(self.tmp, "reports", ".push-scope")
        self.env_backup = dict(os.environ)
        os.environ["WECHAT_WEBHOOK"] = "https://test.example/wechat"
        os.environ["DINGTALK_WEBHOOK"] = "https://test.example/dingtalk"
        os.environ.pop("REVIEW_CHANNEL", None)
        os.environ.pop("SMTP_HOST", None)

        self.calls = []
        self._patches = [
            mock.patch.object(self.pr, "PUSH_FLAG_DIR", self.flag_dir),
            mock.patch.object(self.pr, "PENDING_DIR", self.pending_dir),
            mock.patch.object(self.ps, "SCOPE_DIR", self.scope_dir),
            mock.patch.object(self.pr, "send_wechat",
                              side_effect=lambda t: self.calls.append(("wechat", t))),
            mock.patch.object(self.pr, "send_dingtalk",
                              side_effect=lambda t, title: self.calls.append(("dingtalk", t, title))),
            mock.patch.object(self.pr, "send_mail",
                              side_effect=lambda s, t, p: self.calls.append(("mail", s, t, p))),
            mock.patch.object(self.pr.report, "collect",
                              return_value={"new": []}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        os.environ.clear()
        os.environ.update(self.env_backup)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, args, env_extra=None):
        """按指定 args + env 跑 push_report.main(), 收集 stdout。

        每次调用前先清掉 REVIEW_CHANNEL(除非 env_extra 显式给), 避免上一条用例
        残留的环境变量影响下一条(2026-10-19 闸门加固时踩过)。
        """
        os.environ.pop("REVIEW_CHANNEL", None)
        if env_extra:
            for k, v in env_extra.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        buf = io.StringIO()
        with mock.patch("sys.argv", ["push_report.py"] + args), \
                redirect_stdout(buf):
            try:
                rc = self.pr.main()
            except SystemExit as e:
                rc = e.code
        return rc, buf.getvalue()

    def _declare_broadcast(self, date):
        """模拟定时任务路径: 由调度方先声明"本轮全量"的范围锁。

        2026-10-20 起 --broadcast 不再是"谁喊一声都能全量"的自声明命令:
        必须先有调度路径的范围声明(run_scan.sh --broadcast 或 push_scope.py),
        否则 push_report.py 拒绝执行。
        """
        self.ps.write_scope(date, "broadcast", source="test:cron")
        return date

    def test_backward_compat_no_review_channel_sends_all(self):
        # 2026-10-19 闸门加固: 全量推送改为**显式** --broadcast(默认不扩散),
        # 定时任务的正规入口保持"发所有渠道"。
        # 2026-10-20 起 --broadcast 还必须有调度路径的范围声明(范围锁)。
        self._declare_broadcast("2026-10-19")
        rc, out = self._run(["--date", "2026-10-19", "--broadcast"])
        self.assertEqual(rc, 0, out)
        channels_called = {c[0] for c in self.calls}
        self.assertEqual(channels_called, {"wechat", "dingtalk"}, out)
        # 终态标记: .pushed/<date>.flag
        self.assertTrue(os.path.isfile(
            os.path.join(self.tmp, "reports", ".pushed", "2026-10-19.flag")))
        # 不写 .review-pending
        self.assertFalse(os.path.isfile(
            os.path.join(self.tmp, "reports", ".review-pending", "2026-10-19.flag")))

    def test_review_channel_stage1_sends_only_review(self):
        # 阶段 1: REVIEW_CHANNEL=wechat, 只发微信 + 写 .review-pending
        rc, out = self._run(["--date", "2026-10-19"],
                            env_extra={"REVIEW_CHANNEL": "wechat"})
        self.assertEqual(rc, 0, out)
        channels_called = {c[0] for c in self.calls}
        self.assertEqual(channels_called, {"wechat"}, out)
        # 阶段 1 写 .review-pending, 不写 .pushed
        self.assertTrue(os.path.isfile(
            os.path.join(self.tmp, "reports", ".review-pending", "2026-10-19.flag")))
        self.assertFalse(os.path.isfile(
            os.path.join(self.tmp, "reports", ".pushed", "2026-10-19.flag")))

    def test_review_channel_stage2_skips_without_approval(self):
        # 阶段 1 跑过, 阶段 2 不带 --review-approved → 跳过, 不再发
        self._run(["--date", "2026-10-19"],
                  env_extra={"REVIEW_CHANNEL": "wechat"})
        self.calls.clear()
        rc, out = self._run(["--date", "2026-10-19"],
                            env_extra={"REVIEW_CHANNEL": "wechat"})
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.calls, [], out)
        self.assertIn("待审", out)

    def test_review_channel_stage2_with_approval_sends_remaining(self):
        # 阶段 1: 仅微信
        self._run(["--date", "2026-10-19"],
                  env_extra={"REVIEW_CHANNEL": "wechat"})
        self.calls.clear()
        # 阶段 2: --review-approved → 发钉钉, 写 .pushed, 清理 .review-pending
        rc, out = self._run(["--date", "2026-10-19", "--review-approved"],
                            env_extra={"REVIEW_CHANNEL": "wechat"})
        self.assertEqual(rc, 0, out)
        channels_called = {c[0] for c in self.calls}
        self.assertEqual(channels_called, {"dingtalk"}, out)
        self.assertTrue(os.path.isfile(
            os.path.join(self.tmp, "reports", ".pushed", "2026-10-19.flag")))
        self.assertFalse(os.path.isfile(
            os.path.join(self.tmp, "reports", ".review-pending", "2026-10-19.flag")))

    def test_review_channel_unset_env_falls_back_to_all(self):
        # 真的"没设"审阅渠道 + 显式 --broadcast(定时任务, 已有范围声明)→ 全量发
        self._declare_broadcast("2026-10-19")
        rc, out = self._run(["--date", "2026-10-19", "--broadcast"])
        self.assertEqual(rc, 0, out)
        channels_called = {c[0] for c in self.calls}
        self.assertEqual(channels_called, {"wechat", "dingtalk"}, out)

    def test_review_channel_mail_without_smtp_errors(self):
        # REVIEW_CHANNEL=mail 但 SMTP_HOST 未配 → 报错, 不发任何渠道
        os.environ.pop("SMTP_HOST", None)
        rc, out = self._run(["--date", "2026-10-19"],
                            env_extra={"REVIEW_CHANNEL": "mail"})
        self.assertNotEqual(rc, 0)
        self.assertIn("邮箱", out)
        self.assertIn("SMTP_HOST", out)
        self.assertEqual(self.calls, [])

    def test_review_channel_dingtalk_first_stage(self):
        # REVIEW_CHANNEL=dingtalk, 阶段 1 只发钉钉
        rc, out = self._run(["--date", "2026-10-19"],
                            env_extra={"REVIEW_CHANNEL": "dingtalk"})
        self.assertEqual(rc, 0, out)
        channels_called = {c[0] for c in self.calls}
        self.assertEqual(channels_called, {"dingtalk"}, out)
        self.assertTrue(os.path.isfile(
            os.path.join(self.tmp, "reports", ".review-pending", "2026-10-19.flag")))

    def test_review_approved_with_no_remaining_channels_writes_final_flag(self):
        # 阶段 1 邮件审阅(配 SMTP), 阶段 2 无剩余渠道 → 写终态 + 清理待办
        os.environ["SMTP_HOST"] = "smtp.test"
        os.environ["SMTP_USER"] = "u"
        os.environ["SMTP_PASS"] = "p"
        os.environ["SMTP_TO"] = "x@y"
        os.environ.pop("WECHAT_WEBHOOK", None)
        os.environ.pop("DINGTALK_WEBHOOK", None)
        self._run(["--date", "2026-10-19"],
                  env_extra={"REVIEW_CHANNEL": "mail"})
        self.calls.clear()
        rc, out = self._run(["--date", "2026-10-19", "--review-approved"],
                            env_extra={"REVIEW_CHANNEL": "mail"})
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.calls, [])
        self.assertTrue(os.path.isfile(
            os.path.join(self.tmp, "reports", ".pushed", "2026-10-19.flag")))
        self.assertFalse(os.path.isfile(
            os.path.join(self.tmp, "reports", ".review-pending", "2026-10-19.flag")))

    def test_message_path_respects_review_gate(self):
        # 2026-10-19 晚加固: --message 曾完全绕过闸门(带 REVIEW_CHANNEL 也照样群发),
        # 现在渠道会话里的自定义文本同样只发审阅渠道。
        rc, out = self._run(["--date", "2026-10-19",
                             "--message", "⏰ 提醒"],
                            env_extra={"REVIEW_CHANNEL": "wechat"})
        self.assertEqual(rc, 0, out)
        channels_called = {c[0] for c in self.calls}
        self.assertEqual(channels_called, {"wechat"}, out)
        # --message 不写 .review-pending, 也不写 .pushed
        self.assertFalse(os.path.isfile(
            os.path.join(self.tmp, "reports", ".review-pending", "2026-10-19.flag")))
        self.assertFalse(os.path.isfile(
            os.path.join(self.tmp, "reports", ".pushed", "2026-10-19.flag")))

    def test_stage2_without_review_channel_env_still_excludes_review_channel(self):
        # 2026-10-19 修复的关键回归: 阶段 2 忘设 REVIEW_CHANNEL 时, 原实现静默退化成
        # "全量推送", 审阅渠道(微信)重复收到日报, 闸门形同虚设。
        # 现在阶段 2 以 .review-pending 标记里记录的渠道为准。
        self._run(["--date", "2026-10-19"],
                  env_extra={"REVIEW_CHANNEL": "wechat"})
        self.calls.clear()
        os.environ.pop("REVIEW_CHANNEL", None)   # 阶段 2 忘带环境变量
        rc, out = self._run(["--date", "2026-10-19", "--review-approved"])
        self.assertEqual(rc, 0, out)
        self.assertEqual({c[0] for c in self.calls}, {"dingtalk"}, out)
        self.assertTrue(os.path.isfile(
            os.path.join(self.tmp, "reports", ".pushed", "2026-10-19.flag")))
        self.assertFalse(os.path.isfile(
            os.path.join(self.tmp, "reports", ".review-pending", "2026-10-19.flag")))

    def test_stage2_pending_flag_wins_over_mismatched_env(self):
        # 阶段 1 审的是微信, 阶段 2 环境变量却写成 dingtalk: 必须以标记为准,
        # 否则会把微信(已审阅渠道)又发一遍, 反而把钉钉漏掉。
        self._run(["--date", "2026-10-19"],
                  env_extra={"REVIEW_CHANNEL": "wechat"})
        self.calls.clear()
        rc, out = self._run(["--date", "2026-10-19", "--review-approved"],
                            env_extra={"REVIEW_CHANNEL": "dingtalk"})
        self.assertEqual(rc, 0, out)
        self.assertEqual({c[0] for c in self.calls}, {"dingtalk"}, out)

    def test_pending_flag_records_review_channel(self):
        self._run(["--date", "2026-10-19"],
                  env_extra={"REVIEW_CHANNEL": "wechat"})
        path = os.path.join(self.tmp, "reports", ".review-pending", "2026-10-19.flag")
        with open(path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("review_channel=企业微信", content)
        self.assertEqual(self.pr._pending_channel("2026-10-19"), "企业微信")

    def test_pending_channel_missing_returns_none(self):
        self.assertIsNone(self.pr._pending_channel("2099-01-01"))

    # ---- 2026-10-19 晚: 闸门加固(5 条漏法回归) ----

    def test_unrecognized_review_channel_fails_closed(self):
        # 漏法 1(最致命): 渠道名写错曾被静默丢弃 → 等于"没设闸门" → 全量推送。
        # 现在认不出就报错中止(中文别名与简写已纳入白名单, 见下面那条用例)。
        # 注意: 2026-10-20 起 "weixin" 是**合法**渠道(经 hermes send 投递的
        # 个人微信), 不再是"拼错的名字"的样本, 换成其它手滑写法。
        for bad in ("weixn", "unknown-channel", "企业微信群", "corp-wechat"):
            self.calls.clear()
            rc, out = self._run(["--date", "2026-10-30"],
                                env_extra={"REVIEW_CHANNEL": bad})
            self.assertNotEqual(rc, 0, (bad, out))
            self.assertIn("无法识别", out)
            self.assertEqual(self.calls, [], (bad, out))

    def test_review_channel_accepts_chinese_alias(self):
        # 渠道名认得出就应当按中文别名工作(而不是报错或静默全量)
        # 每个别名用不同日期, 避免被"同日幂等"标记挡掉
        aliases = [a for a in ("企业微信", "wx", "wecom")
                   if a in self.pr.REVIEW_CHANNEL_ALIASES]
        for i, alias in enumerate(aliases):
            self.calls.clear()
            day = "2026-11-%02d" % (i + 1)
            rc, out = self._run(["--date", day],
                                env_extra={"REVIEW_CHANNEL": alias})
            self.assertEqual(rc, 0, (alias, out))
            self.assertEqual({c[0] for c in self.calls}, {"wechat"}, (alias, out))

    def test_review_channel_cli_flag_works(self):
        # 显式参数优先于环境变量, 且渠道在命令行里可见可审计
        self.calls.clear()
        rc, out = self._run(["--date", "2026-10-31", "--review-channel", "wechat"])
        self.assertEqual(rc, 0, out)
        self.assertEqual({c[0] for c in self.calls}, {"wechat"}, out)

    def test_force_cannot_escape_pending_review_gate(self):
        # 漏法 2: 阶段 1 落待审后, --force 且忘带渠道参数曾直接全量重发 → 钉钉收到
        self._run(["--date", "2026-10-20"],
                  env_extra={"REVIEW_CHANNEL": "wechat"})
        self.calls.clear()
        rc, out = self._run(["--date", "2026-10-20", "--force"])
        self.assertEqual(rc, 0, out)
        self.assertEqual({c[0] for c in self.calls}, {"wechat"}, out)
        self.assertNotIn("dingtalk", {c[0] for c in self.calls}, out)

    def test_message_path_respects_review_channel(self):
        # 漏法 3: --message 曾完全绕过闸门, 带 REVIEW_CHANNEL 也照样群发
        self.calls.clear()
        rc, out = self._run(["--date", "2026-10-30", "--message", "⏰ 提醒"],
                            env_extra={"REVIEW_CHANNEL": "wechat"})
        self.assertEqual(rc, 0, out)
        self.assertEqual({c[0] for c in self.calls}, {"wechat"}, out)

    def test_message_path_respects_pending_gate(self):
        # 漏法 4: 待审期间 --message 提醒仍群发到钉钉
        self._run(["--date", "2026-10-21"],
                  env_extra={"REVIEW_CHANNEL": "wechat"})
        self.calls.clear()
        rc, out = self._run(["--date", "2026-10-21", "--message", "⏰ 提醒"])
        self.assertEqual(rc, 0, out)
        self.assertEqual({c[0] for c in self.calls}, {"wechat"}, out)

    def test_message_without_gate_still_broadcasts(self):
        # 正常 cron fallback 的"待执行提醒"仍应群发(没有待审标记, 也没指定渠道)
        self.calls.clear()
        rc, out = self._run(["--date", "2026-10-30", "--message", "⏰ 提醒"])
        self.assertEqual(rc, 0, out)
        self.assertEqual({c[0] for c in self.calls}, {"wechat", "dingtalk"}, out)

    def test_review_channel_not_configured_fails_loudly(self):
        # 审阅渠道没配 = 一条都发不出去, 要报错而不是静默
        os.environ.pop("WECHAT_WEBHOOK", None)
        self.calls.clear()
        rc, out = self._run(["--date", "2026-10-30", "--review-channel", "wechat"])
        self.assertNotEqual(rc, 0, out)
        self.assertIn("未配置", out)
        self.assertEqual(self.calls, [], out)

    def test_no_scope_does_not_push_at_all(self):
        # 漏法 5(结构性): 原来"没指定渠道 = 全量发", 于是任何一次忘了指定的调用
        # 都会把日报发到所有渠道。现在默认**不推送**, 扩散必须显式 --broadcast。
        self.calls.clear()
        rc, out = self._run(["--date", "2026-10-30"])
        self.assertNotEqual(rc, 0, out)
        self.assertIn("未指定推送范围", out)
        self.assertEqual(self.calls, [], out)

    def test_broadcast_sends_all_channels_explicitly(self):
        # 定时任务的全量推送: 显式 --broadcast 仍然全量发(向后兼容的正规入口)
        self._declare_broadcast("2026-10-30")
        self.calls.clear()
        rc, out = self._run(["--date", "2026-10-30", "--broadcast"])
        self.assertEqual(rc, 0, out)
        self.assertEqual({c[0] for c in self.calls}, {"wechat", "dingtalk"}, out)

    def test_broadcast_and_review_channel_are_mutually_exclusive(self):
        # "只发微信"与"发所有渠道"不能同时声明
        self.calls.clear()
        rc, out = self._run(["--date", "2026-10-30", "--broadcast", "--review-channel", "wechat"])
        self.assertNotEqual(rc, 0, out)
        self.assertIn("互斥", out)
        self.assertEqual(self.calls, [], out)

    def test_broadcast_blocked_by_pending_gate(self):
        # 待审期间 --broadcast 不能扩散。
        # 2026-10-20 起范围锁把这道门从"降级成只发审阅渠道"改成"直接拒绝":
        # 降级会让人以为全量推送已经完成, 实际只补发了一次审阅渠道。
        self._run(["--date", "2026-10-22"], env_extra={"REVIEW_CHANNEL": "wechat"})
        self.calls.clear()
        rc, out = self._run(["--date", "2026-10-22", "--force", "--broadcast"])
        self.assertNotEqual(rc, 0, out)
        self.assertEqual(self.calls, [], out)
        self.assertIn("范围锁", out)

    def test_pending_gate_blocks_resend_without_force(self):
        # 待审期间再跑一次(无 --force): 幂等早退, 一条都不发
        self._run(["--date", "2026-10-23"], env_extra={"REVIEW_CHANNEL": "wechat"})
        self.calls.clear()
        rc, out = self._run(["--date", "2026-10-23"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.calls, [], out)
        self.assertIn("待审", out)

    def test_dry_run_without_channels_says_channel_config_not_review_gate(self):
        # 2026-10-19 修正: 一个推送渠道都没配时, dry-run 原来打的是
        # "评审核闸门开启但剩余渠道无配置可发" —— 闸门根本没开, 这条提示会把人带偏
        # (以为被闸门挡住了, 实际是没配渠道)。
        os.environ.pop("WECHAT_WEBHOOK", None)
        os.environ.pop("DINGTALK_WEBHOOK", None)
        self._declare_broadcast("2026-10-19")
        rc, out = self._run(["--date", "2026-10-19", "--broadcast", "--dry-run"])
        self.assertIn("未配置任何推送渠道", out)
        self.assertNotIn("评审核闸门开启", out)

    def test_stage2_without_any_review_channel_is_refused(self):
        # 2026-10-20 修复: 阶段 2 读不到审阅渠道(无待审标记、无范围锁)时,
        # 原实现只"警告一下"就照常全量发 —— 又一个"忘了 → 发到所有渠道"的口子。
        # 现在直接拒绝: 没有待审记录就没有"批准"的对象。
        rc, out = self._run(["--date", "2026-10-19", "--review-approved"])
        self.assertNotEqual(rc, 0, out)
        self.assertIn("无待审记录", out)
        self.assertEqual(self.calls, [], out)

    def test_stage2_with_env_or_lock_but_no_pending_is_refused(self):
        # 2026-10-24 体检 H5: 原实现 review_label = pending or lock or env 的兜底链
        # 让"无待审记录即拒绝"只在三者全空时生效 —— run_scan.sh --review-channel
        # 路径既 export REVIEW_CHANNEL 又写 review 锁, Agent 跳过阶段 1 直接
        # --review-approved 就能向其余渠道群发(未审先扩散)。现在阶段 2 只认
        # .review-pending 标记(它是"审阅版已发出"的唯一凭证)。
        # 场景 1: 有 REVIEW_CHANNEL 环境变量, 无待审标记
        rc, out = self._run(["--date", "2026-10-19", "--review-approved"],
                            env_extra={"REVIEW_CHANNEL": "wechat"})
        self.assertNotEqual(rc, 0, out)
        self.assertIn("无待审记录", out)
        self.assertEqual(self.calls, [], out)
        # 场景 2: 有 review 范围锁(run_scan.sh --review-channel 会写), 无待审标记
        self.ps.write_scope("2026-10-19", "review", channel="wechat",
                            source="run_scan.sh:policy:session")
        self.calls.clear()
        rc, out = self._run(["--date", "2026-10-19", "--review-approved"],
                            env_extra={"REVIEW_CHANNEL": "wechat"})
        self.assertNotEqual(rc, 0, out)
        self.assertIn("无待审记录", out)
        self.assertEqual(self.calls, [], out)
        # 场景 3: 显式 --review-channel 与 --review-approved 组合同样不兜底
        self.calls.clear()
        rc, out = self._run(["--date", "2026-10-19", "--review-approved",
                             "--review-channel", "wechat"])
        self.assertNotEqual(rc, 0, out)
        self.assertEqual(self.calls, [], out)

    def test_message_with_review_approved_is_mutually_exclusive(self):
        # 2026-10-24 体检 H5: --review-approved 的"批准对象"只能是已发出的审阅版
        # 日报; 自定义文本没有对应待审对象, 组合即拒绝, 不给"未审先扩散"留旁路。
        rc, out = self._run(["--date", "2026-10-19", "--review-approved",
                             "--message", "自定义文本"])
        self.assertNotEqual(rc, 0, out)
        self.assertIn("互斥", out)
        self.assertEqual(self.calls, [], out)

    def test_push_report_loads_channels_from_hermes_env(self):
        # 2026-10-20 生产现场: 人工在普通 shell 里跑 push_report.py 时, 渠道变量
        # 不在环境里(只有 run_scan.sh 会 source ~/.hermes/.env) →
        # "未配置任何推送渠道", 白跑一趟。现在脚本自己从 .env 补齐。
        import push_scope
        env_path = os.path.join(self.tmp, ".hermes", ".env")
        os.makedirs(os.path.dirname(env_path), exist_ok=True)
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("DINGTALK_WEBHOOK=https://oapi.dingtalk.com/robot/send?x=1\n")
        os.environ.pop("DINGTALK_WEBHOOK", None)
        os.environ.pop("WECHAT_WEBHOOK", None)
        self.ps.write_scope("2026-10-29", "broadcast", source="unit")
        self.calls.clear()
        buf = io.StringIO()
        with mock.patch.object(push_scope, "HERMES_ENV_PATH", env_path), \
                mock.patch("sys.argv", ["push_report.py", "--date", "2026-10-29",
                                         "--broadcast"]), \
                redirect_stdout(buf):
            rc = self.pr.main()
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertEqual({c[0] for c in self.calls}, {"dingtalk"}, buf.getvalue())


class PushScopeLockTests(unittest.TestCase):
    """推送范围锁(2026-10-20 钉钉误推事故后引入)。

    事故形态: run_scan.sh 的 prompt 把"渠道会话 vs 定时任务"交给 Agent 临场判断,
    而 run_scan.sh 是 cron 与手动补跑**共用**的入口 —— 手动补跑在 prompt 眼里
    就是"定时任务那条路", 于是 Agent 选了 --broadcast, 日报发到了钉钉;
    且 run_scan.sh 里**根本没有 REVIEW_CHANNEL 传递机制**。

    现在: 谁发起由发起方显式声明并落盘成范围锁, push_report.py 读锁执行。
    """

    def setUp(self):
        import push_report
        import push_scope
        self.pr = push_report
        self.ps = push_scope
        self.tmp = tempfile.mkdtemp(prefix="push-scope-")
        self.flag_dir = os.path.join(self.tmp, "reports", ".pushed")
        self.pending_dir = os.path.join(self.tmp, "reports", ".review-pending")
        self.scope_dir = os.path.join(self.tmp, "reports", ".push-scope")
        self.env_backup = dict(os.environ)
        os.environ["WECHAT_WEBHOOK"] = "https://test.example/wechat"
        os.environ["DINGTALK_WEBHOOK"] = "https://test.example/dingtalk"
        os.environ.pop("REVIEW_CHANNEL", None)
        os.environ.pop("SMTP_HOST", None)

        self.calls = []
        self._patches = [
            mock.patch.object(self.pr, "PUSH_FLAG_DIR", self.flag_dir),
            mock.patch.object(self.pr, "PENDING_DIR", self.pending_dir),
            mock.patch.object(self.ps, "SCOPE_DIR", self.scope_dir),
            mock.patch.object(self.pr, "send_wechat",
                              side_effect=lambda t: self.calls.append(("wechat", t))),
            mock.patch.object(self.pr, "send_dingtalk",
                              side_effect=lambda t, title: self.calls.append(("dingtalk", t, title))),
            mock.patch.object(self.pr.report, "collect",
                              return_value={"new": []}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        os.environ.clear()
        os.environ.update(self.env_backup)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, args, env_extra=None):
        os.environ.pop("REVIEW_CHANNEL", None)
        if env_extra:
            for k, v in env_extra.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        buf = io.StringIO()
        with mock.patch("sys.argv", ["push_report.py"] + args), \
                redirect_stdout(buf):
            try:
                rc = self.pr.main()
            except SystemExit as e:
                rc = e.code
        return rc, buf.getvalue()

    # ---- 范围锁本身 ----

    def test_scope_lock_roundtrip(self):
        self.ps.write_scope("2026-10-20", "review", channel="wechat", source="unit")
        scope = self.ps.read_scope("2026-10-20")
        self.assertEqual(scope["scope"], "review")
        self.assertEqual(scope["channel"], "wechat")
        self.assertEqual(scope["channel_label"], "企业微信")
        self.assertEqual(self.ps.describe_scope(scope), "只发 企业微信")
        self.assertTrue(self.ps.clear_scope("2026-10-20"))
        self.assertIsNone(self.ps.read_scope("2026-10-20"))

    def test_push_scope_cli_rejects_unknown_channel(self):
        # 渠道名认不出 → 报错退出(fail-closed), 绝不静默当成"没设闸门"
        # (注意: "weixin" 自 2026-10-20 起是合法渠道, 不再是拼错的名字)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = self.ps.main(["--date", "2026-10-20", "--review-channel", "weixn"])
        self.assertEqual(rc, 1)
        self.assertIn("无法识别", buf.getvalue())
        self.assertIsNone(self.ps.read_scope("2026-10-20"))

    def test_push_scope_cli_write_show_clear(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = self.ps.main(["--date", "2026-10-20", "--broadcast", "--source", "unit"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.ps.read_scope("2026-10-20")["scope"], "broadcast")
        with redirect_stdout(io.StringIO()):
            rc = self.ps.main(["--date", "2026-10-20", "--show"])
        self.assertEqual(rc, 0)
        with redirect_stdout(io.StringIO()):
            rc = self.ps.main(["--date", "2026-10-20", "--clear"])
        self.assertEqual(rc, 0)
        self.assertIsNone(self.ps.read_scope("2026-10-20"))

    def test_broadcast_declaration_requires_source(self):
        # 10-20 现场的锁是 source=manual(默认值) → 说明有人随手敲了
        # push_scope.py --broadcast。现在全量声明必须写明依据, 留空即拒绝。
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = self.ps.main(["--date", "2026-10-20", "--broadcast"])
        self.assertEqual(rc, 1)
        self.assertIn("--source", buf.getvalue())
        self.assertIsNone(self.ps.read_scope("2026-10-20"))

    def test_push_scope_label_only(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = self.ps.main(["--print-label", "--review-channel", "企业微信"])
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue().strip(), "企业微信")

    # ---- 闸门执行范围锁 ----

    def test_broadcast_without_scope_declaration_is_refused(self):
        # 事故核心: --broadcast 曾是"谁喊一声都能全量"的自声明命令。
        # 现在没有调度路径的范围声明 → 拒绝, 绝不发到所有渠道。
        rc, out = self._run(["--date", "2026-10-20", "--broadcast"])
        self.assertNotEqual(rc, 0, out)
        self.assertEqual(self.calls, [], out)
        self.assertIn("范围声明", out)

    def test_broadcast_refused_when_lock_says_review_only(self):
        # 阶段 1 已把本轮锁成"只发微信", 之后的 --broadcast 必须被拒
        self._run(["--date", "2026-10-21", "--review-channel", "wechat"])
        self.calls.clear()
        rc, out = self._run(["--date", "2026-10-21", "--broadcast", "--force"])
        self.assertNotEqual(rc, 0, out)
        self.assertEqual(self.calls, [], out)
        self.assertIn("范围锁", out)

    def test_broadcast_allowed_with_broadcast_declaration(self):
        # 定时任务路径(run_scan.sh --broadcast 或 push_scope.py)声明后 → 全量
        self.ps.write_scope("2026-10-22", "broadcast", source="run_scan.sh:policy")
        rc, out = self._run(["--date", "2026-10-22", "--broadcast"])
        self.assertEqual(rc, 0, out)
        self.assertEqual({c[0] for c in self.calls}, {"wechat", "dingtalk"}, out)

    def test_stage1_writes_review_lock(self):
        rc, out = self._run(["--date", "2026-10-23", "--review-channel", "wechat"])
        self.assertEqual(rc, 0, out)
        scope = self.ps.read_scope("2026-10-23")
        self.assertEqual(scope["scope"], "review")
        self.assertEqual(scope["channel"], "wechat")

    def test_stage2_clears_lock_after_approval(self):
        self._run(["--date", "2026-10-24", "--review-channel", "wechat"])
        self.calls.clear()
        rc, out = self._run(["--date", "2026-10-24", "--review-approved"])
        self.assertEqual(rc, 0, out)
        self.assertEqual({c[0] for c in self.calls}, {"dingtalk"}, out)
        # 本轮已扩散完毕 → 范围锁失效, 同日再喊 --broadcast 也不会被误挡
        self.assertIsNone(self.ps.read_scope("2026-10-24"))

    def test_conflicting_review_channel_with_lock_is_refused(self):
        # 同一轮请求不得改投其它渠道: 锁是微信, 命令行却写钉钉 → 拒绝
        self.ps.write_scope("2026-10-25", "review", channel="wechat", source="unit")
        rc, out = self._run(["--date", "2026-10-25", "--review-channel", "dingtalk"])
        self.assertNotEqual(rc, 0, out)
        self.assertEqual(self.calls, [], out)
        self.assertIn("不一致", out)

    def test_message_path_respects_review_lock(self):
        # --message 也要受范围锁约束(自定义文本不得扩散)
        self.ps.write_scope("2026-10-26", "review", channel="wechat", source="unit")
        rc, out = self._run(["--date", "2026-10-26", "--message", "⏰ 提醒"])
        self.assertEqual(rc, 0, out)
        self.assertEqual({c[0] for c in self.calls}, {"wechat"}, out)

    def test_message_path_writes_review_lock(self):
        # 自定义文本也要把范围落成锁, 否则本轮之后一次 --broadcast 仍能自我放行
        rc, out = self._run(["--date", "2026-10-27", "--review-channel", "wechat",
                              "--message", "⏰ 提醒"])
        self.assertEqual(rc, 0, out)
        scope = self.ps.read_scope("2026-10-27")
        self.assertEqual(scope["scope"], "review")
        self.assertEqual(scope["channel"], "wechat")

    def test_dry_run_does_not_write_lock(self):
        # dry-run 是只读预演, 不该留状态
        rc, out = self._run(["--date", "2026-10-28", "--review-channel", "wechat",
                              "--dry-run"])
        self.assertEqual(rc, 0, out)
        self.assertIsNone(self.ps.read_scope("2026-10-28"))

    # ---- 编排层(run_scan.sh)静态回归 ----

    def _run_scan_text(self):
        with open(os.path.join(REPO, "scripts", "run_scan.sh"), encoding="utf-8") as f:
            return f.read()

    def test_run_scan_no_longer_asks_agent_to_judge_channel(self):
        # 事故的直接成因: prompt 里让 Agent 自己判断"渠道会话还是定时任务"。
        # 这段措辞必须从 run_scan.sh 里消失。
        text = self._run_scan_text()
        self.assertNotIn("若本轮是用户在某个渠道发起的请求", text)
        self.assertNotIn("才用 python scripts/push_report.py", text)

    def test_run_scan_declares_scope_and_passes_review_channel(self):
        # run_scan.sh 必须: ①写范围锁 ②把渠道真正传给下游(REVIEW_CHANNEL)
        text = self._run_scan_text()
        self.assertIn("push_scope.py", text)
        self.assertIn("--review-channel", text)
        self.assertIn("export REVIEW_CHANNEL=", text)

    def test_run_scan_declares_scope_before_calling_hermes(self):
        # 顺序回归: 范围锁必须在 HERMES_RUN_CMD 之前落盘, 否则 Agent 推第一下时
        # 锁还不存在, --broadcast 仍能自我放行。
        text = self._run_scan_text()
        # 取"真正调用 Hermes"那一处, 而不是文件头注释里的 HERMES_RUN_CMD
        self.assertLess(text.index("push_scope.py"),
                        text.index('if [ -n "${HERMES_RUN_CMD:-}" ]'))

    def test_run_scan_broadcast_requires_cron_context(self):
        # 显式 --broadcast 也必须发生在 cron 上下文: 手动补跑与 cron 共用入口,
        # 允许它自称定时任务, 就等于把"谁发起的"又交回给调用方(10-20 事故形态)。
        text = self._run_scan_text()
        self.assertIn('_is_cron_context', text)
        self.assertIn('SCOPE" = "broadcast" ] && ! _is_cron_context', text)

    def test_card_carries_code_fingerprint(self):
        # 推送内容带代码指纹: 事故现场一眼看出"跑的是哪版代码"
        import push_report
        text = push_report.brief_text("2026-10-20", {
            "new": []})
        self.assertIn("代码", text)

    def test_diagnose_flags_scope_none_in_log(self):
        # 诊断脚本要能发现"cron 上下文没识别出来 → 本轮没推送"。
        # 注意样本必须是**自称定时任务**的来源(src=显式参数/cron自动识别);
        # 手动触发的 scope=none 是正确行为, 另有一条用例守着。
        import diagnose_push
        log = os.path.join(self.tmp, "cron.log")
        with open(log, "w", encoding="utf-8") as f:
            f.write("[2026-10-20 07:30:00] run_scan mode=policy date=2026-10-20 "
                    "tz=Asia/Shanghai scope=none src=显式参数 start\n")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = diagnose_push.main(["--date", "2026-10-20", "--log", log,
                                      "--log-tail", "50"])
        out = buf.getvalue()
        self.assertIn("scope=none", out)
        self.assertNotEqual(rc, 0)

    def test_diagnose_finds_scan_log_by_default(self):
        # 生产实际的日志叫 scan.log(不是早期文档里的 cron.log), 默认要能找到
        import diagnose_push
        log_dir = os.path.join(self.tmp, "logs")
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "scan.log"), "w", encoding="utf-8") as f:
            f.write("[2026-10-20 23:30:00] run_scan mode=policy date=2026-10-20 "
                    "scope=broadcast src=显式参数 start\n")
        buf = io.StringIO()
        with mock.patch.object(diagnose_push, "LOG_DIR", log_dir), \
                redirect_stdout(buf):
            diagnose_push.main(["--date", "2026-10-20", "--log-tail", "50"])
        out = buf.getvalue()
        self.assertIn("scan.log", out)
        self.assertIn("scope=broadcast", out)

    def test_diagnose_no_false_alarm_when_code_has_scope(self):
        # 当前代码已带范围声明时, 日志里没有 scope= 只能说明"最近那次跑批早于本次
        # 部署" —— 属预期, 不该报成"生产在跑旧版"(2026-10-20 部署完自检误报)。
        import diagnose_push
        log = os.path.join(self.tmp, "cron.log")
        with open(log, "w", encoding="utf-8") as f:
            f.write("[2026-10-20 07:30:00] run_scan mode=policy date=2026-10-20 "
                    "tz=Asia/Shanghai start\n")
        buf = io.StringIO()
        with redirect_stdout(buf):
            diagnose_push.main(["--date", "2026-10-20", "--log", log, "--log-tail", "50"])
        out = buf.getvalue()
        self.assertNotIn("旧版 run_scan.sh", out)
        self.assertIn("下一次跑批起就会有", out)

    def test_diagnose_warns_when_code_lacks_scope(self):
        # 反面: 当前代码确实没有范围声明(生产真的在跑旧版) → 必须报警
        import diagnose_push
        log = os.path.join(self.tmp, "cron.log")
        with open(log, "w", encoding="utf-8") as f:
            f.write("[2026-10-20 07:30:00] run_scan mode=policy date=2026-10-20 start\n")
        buf = io.StringIO()
        with mock.patch.object(diagnose_push, "_current_run_scan_has_scope", return_value=False), \
                redirect_stdout(buf):
            rc = diagnose_push.main(["--date", "2026-10-20", "--log", log, "--log-tail", "50"])
        out = buf.getvalue()
        self.assertIn("[警告]", out)
        self.assertIn("旧版", out)
        self.assertNotEqual(rc, 0)

    def test_diagnose_manual_scope_none_is_not_a_problem(self):
        # 手动/渠道会话触发的 scope=none 是**正确行为**(非 cron → 不扩散),
        # 不能报成"cron 识别失败"(2026-10-22 误报)
        import diagnose_push
        log = os.path.join(self.tmp, "cron.log")
        with open(log, "w", encoding="utf-8") as f:
            f.write("[2026-10-22 08:49:08] run_scan mode=policy date=2026-10-22 "
                    "tz=Asia/Shanghai scope=none src=未声明(非定时任务上下文) start\n")
        buf = io.StringIO()
        with redirect_stdout(buf):
            diagnose_push.main(["--date", "2026-10-22", "--log", log, "--log-tail", "50"])
        out = buf.getvalue()
        self.assertIn("不扩散是正确的行为", out)
        self.assertNotIn("cron 识别失败", out)

    def test_diagnose_cron_scope_none_is_still_flagged(self):
        # 反面: 来源自称定时任务却判成 none → 仍是矛盾, 必须报警
        import diagnose_push
        log = os.path.join(self.tmp, "cron.log")
        with open(log, "w", encoding="utf-8") as f:
            f.write("[2026-10-22 07:30:01] run_scan mode=policy date=2026-10-22 "
                    "tz=Asia/Shanghai scope=none src=显式参数 start\n")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = diagnose_push.main(["--date", "2026-10-22", "--log", log, "--log-tail", "50"])
        out = buf.getvalue()
        self.assertIn("cron 识别失败", out)
        self.assertNotEqual(rc, 0)

    def test_diagnose_reads_hermes_env_for_channel_config(self):
        # 渠道配置在 ~/.hermes/.env 里, 手动跑诊断时 shell 里没有 → 必须去读,
        # 否则会误报"渠道未配置"(10-20 现场差点据此误判)
        import diagnose_push
        import push_scope
        env_path = os.path.join(self.tmp, ".hermes", ".env")
        os.makedirs(os.path.dirname(env_path), exist_ok=True)
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("DINGTALK_WEBHOOK=https://oapi.dingtalk.com/robot/send?x=1\n")
        os.environ.pop("WECHAT_WEBHOOK", None)
        os.environ.pop("DINGTALK_WEBHOOK", None)
        os.environ.pop("SMTP_HOST", None)
        buf = io.StringIO()
        with mock.patch.object(push_scope, "HERMES_ENV_PATH", env_path), \
                mock.patch.object(diagnose_push, "_run", return_value=(None, "not found")), \
                redirect_stdout(buf):
            diagnose_push.main(["--date", "2026-10-20"])
        out = buf.getvalue()
        self.assertIn("钉钉", out)
        self.assertNotIn("钉钉       未配置", out)
        # 只配了一个渠道 → 必须提示"只发微信审阅"在当前配置下跑不通
        self.assertIn("只配了", out)


class PushHermesChannelTests(unittest.TestCase):
    """Hermes 投递型渠道(个人微信/飞书)—— 2026-10-20 生产现场新增。

    现场事实: 生产 .env 里**没有** WECHAT_WEBHOOK(企业微信群机器人), 用户说的
    "微信会话"是 Hermes 的个人微信通道(WEIXIN_*)。脚本自己 POST 发不进去,
    只有借 `hermes send` 才能把日报送进用户与 Hermes 说话的那个会话。
    """

    def setUp(self):
        import push_report
        import push_scope
        self.pr = push_report
        self.ps = push_scope
        self.tmp = tempfile.mkdtemp(prefix="push-hermes-")
        self.flag_dir = os.path.join(self.tmp, "reports", ".pushed")
        self.pending_dir = os.path.join(self.tmp, "reports", ".review-pending")
        self.scope_dir = os.path.join(self.tmp, "reports", ".push-scope")
        self.env_backup = dict(os.environ)
        os.environ["WECHAT_WEBHOOK"] = "https://test.example/wechat"
        os.environ["DINGTALK_WEBHOOK"] = "https://test.example/dingtalk"
        os.environ.pop("SMTP_HOST", None)
        os.environ.pop("REVIEW_CHANNEL", None)
        os.environ.pop("HERMES_PUSH_CHANNELS", None)

        self.calls = []
        self.hermes_calls = []
        # push_report 直接 from push_scope import channel_available,
        # 所以两个模块的引用都要打桩, 否则 patch 不到实际生效的那一份。
        self._real_send_hermes = push_report.send_hermes
        self._patches = [
            mock.patch.object(self.pr, "PUSH_FLAG_DIR", self.flag_dir),
            mock.patch.object(self.pr, "PENDING_DIR", self.pending_dir),
            mock.patch.object(self.ps, "SCOPE_DIR", self.scope_dir),
            mock.patch.object(self.pr, "send_wechat",
                              side_effect=lambda t: self.calls.append(("wechat", t))),
            mock.patch.object(self.pr, "send_dingtalk",
                              side_effect=lambda t, title: self.calls.append(("dingtalk", t, title))),
            mock.patch.object(self.pr, "send_hermes",
                              side_effect=self._fake_hermes),
            mock.patch.object(self.pr, "channel_available",
                              side_effect=self._fake_available),
            mock.patch.object(self.ps, "channel_available",
                              side_effect=self._fake_available),
            mock.patch.object(self.pr.report, "collect",
                              return_value={"new": []}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        os.environ.clear()
        os.environ.update(self.env_backup)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_hermes(self, text, platform, docx=None):
        self.hermes_calls.append((platform, text, docx))
        return {"ok": True}

    def _fake_available(self, key):
        # 只让 weixin 可用(feishu 没配)
        return key == "weixin"

    def _run(self, args, env_extra=None):
        os.environ.pop("REVIEW_CHANNEL", None)
        if env_extra:
            for k, v in env_extra.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        buf = io.StringIO()
        with mock.patch("sys.argv", ["push_report.py"] + args), \
                redirect_stdout(buf):
            try:
                rc = self.pr.main()
            except SystemExit as e:
                rc = e.code
        return rc, buf.getvalue()

    # ---- 别名与解析 ----

    def test_alias_weixin_variants(self):
        for alias in ("weixin", "微信", "个人微信"):
            self.assertEqual(self.ps.resolve_channel(alias), "weixin", alias)

    def test_alias_feishu_variants(self):
        for alias in ("feishu", "飞书", "lark"):
            self.assertEqual(self.ps.resolve_channel(alias), "feishu", alias)

    def test_enterprise_wechat_alias_unchanged(self):
        # "企业微信"仍是群机器人(webhook 型), 与"微信"(个人微信/Hermes)不是一回事
        self.assertEqual(self.ps.resolve_channel("企业微信"), "wechat")
        self.assertEqual(self.ps.resolve_channel("微信"), "weixin")

    def test_channel_kind_and_label(self):
        self.assertEqual(self.ps.channel_kind("weixin"), "hermes")
        self.assertEqual(self.ps.channel_kind("dingtalk"), "webhook")
        self.assertEqual(self.ps.channel_label("weixin"), "微信")
        self.assertEqual(self.ps.channel_key_for_label("微信"), "weixin")

    # ---- 推送行为 ----

    def test_review_channel_weixin_sends_only_weixin(self):
        # 阶段 1 显式指定微信 → 只发微信, 钉钉一条都不发
        rc, out = self._run(["--date", "2026-12-01", "--review-channel", "weixin"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.calls, [], out)
        self.assertEqual([c[0] for c in self.hermes_calls], ["weixin"], out)

    def test_review_channel_weixin_writes_review_lock(self):
        rc, out = self._run(["--date", "2026-12-02", "--review-channel", "weixin"])
        self.assertEqual(rc, 0, out)
        scope = self.ps.read_scope("2026-12-02")
        self.assertEqual(scope["scope"], "review")
        self.assertEqual(scope["channel"], "weixin")
        self.assertEqual(scope["channel_label"], "微信")

    def test_broadcast_does_not_include_weixin_by_default(self):
        # 默认全量集合 = 已配 webhook 渠道(钉钉); 微信不被顺带发出去,
        # 免得日报突然开始刷屏用户微信
        self.ps.write_scope("2026-12-03", "broadcast", source="test:cron")
        rc, out = self._run(["--date", "2026-12-03", "--broadcast"])
        self.assertEqual(rc, 0, out)
        self.assertEqual({c[0] for c in self.calls}, {"wechat", "dingtalk"}, out)
        self.assertEqual(self.hermes_calls, [], out)

    def test_hermes_push_channels_env_opts_in(self):
        # 显式打开后, 全量推送才带上微信
        os.environ["HERMES_PUSH_CHANNELS"] = "weixin"
        self.ps.write_scope("2026-12-04", "broadcast", source="test:cron")
        rc, out = self._run(["--date", "2026-12-04", "--broadcast"])
        self.assertEqual(rc, 0, out)
        self.assertEqual({c[0] for c in self.calls}, {"wechat", "dingtalk"}, out)
        self.assertEqual([c[0] for c in self.hermes_calls], ["weixin"], out)

    def test_stage2_after_weixin_review_sends_remaining_only(self):
        # 阶段 1 只发微信 → 阶段 2 发完其余渠道(钉钉), 微信不重复收
        self._run(["--date", "2026-12-05", "--review-channel", "weixin"])
        self.calls.clear()
        self.hermes_calls.clear()
        rc, out = self._run(["--date", "2026-12-05", "--review-approved"])
        self.assertEqual(rc, 0, out)
        self.assertEqual({c[0] for c in self.calls}, {"wechat", "dingtalk"}, out)
        self.assertEqual(self.hermes_calls, [], out)

    def test_weixin_review_blocked_when_hermes_missing(self):
        # hermes CLI 不可用 → 审阅渠道不可用 → 报错中止, 不得退回"发所有渠道"
        with mock.patch.object(self.pr, "channel_available", return_value=False):
            rc, out = self._run(["--date", "2026-12-06", "--review-channel", "weixin"])
        self.assertNotEqual(rc, 0, out)
        self.assertIn("不可用", out)
        self.assertEqual(self.calls, [], out)
        self.assertEqual(self.hermes_calls, [], out)

    def test_send_hermes_uses_stdin_and_attaches_docx(self):
        # send_hermes: 走 stdin(--file -), docx 存在时正文追加 MEDIA:<path>
        import tempfile as _tf
        docx = os.path.join(self.tmp, "r.docx")
        with open(docx, "wb") as f:
            f.write(b"x")
        rec = {}

        def fake_run(cmd, **kw):
            rec["cmd"] = cmd
            rec["input"] = kw.get("input")
            class P:
                returncode = 0
                stdout = b"ok"
                stderr = b""
            return P()

        with mock.patch.object(self.pr.subprocess, "run", fake_run):
            self._real_send_hermes("日报正文", "weixin", docx)
        self.assertEqual(rec["cmd"][:2], ["hermes", "send"])
        self.assertIn("--to", rec["cmd"])
        self.assertEqual(rec["cmd"][rec["cmd"].index("--to") + 1], "weixin")
        body = rec["input"].decode("utf-8")
        self.assertIn("日报正文", body)
        self.assertIn("MEDIA:%s" % docx, body)

    def test_send_hermes_raises_on_failure(self):
        def fake_run(cmd, **kw):
            class P:
                returncode = 1
                stdout = b""
                stderr = "投递失败".encode("utf-8")
            return P()

        with mock.patch.object(self.pr.subprocess, "run", fake_run):
            with self.assertRaises(RuntimeError) as ctx:
                self._real_send_hermes("正文", "weixin", None)
        self.assertIn("投递失败", str(ctx.exception))


class ReportAttachmentTests(unittest.TestCase):
    """Word 附件闭环(2026-10-20 生产现场发现)。

    现场事实: 10-20 的跑批**漏掉了 `report.py --docx`**(只产出 csv/md/xlsx),
    而 push_report.py 侧是 `if isfile(docx_path)` **静默跳过附件** ——
    用户收到纯文本, 以为带 Word, 事后从推送记录里完全看不出来。

    2026-10-25 调整: Excel/Word 改为按需人工生成 —— 跑批只产 Markdown, 推送
    不再自动补 Word(有则带附件, 无则正文标注"无 Word 附件", 渠道侧不静默);
    report.ensure_docx 保留为人工按需入口(幂等, md 缺失不补防空壳)。
    """

    def setUp(self):
        import push_report
        import push_scope
        import report
        self.pr = push_report
        self.ps = push_scope
        self.report = report
        self.tmp = tempfile.mkdtemp(prefix="report-attach-")
        self.env_backup = dict(os.environ)
        os.environ["DINGTALK_WEBHOOK"] = "https://test.example/dingtalk"
        os.environ.pop("REVIEW_CHANNEL", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env_backup)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- report.ensure_docx ----

    def test_ensure_docx_noop_when_present(self):
        os.makedirs(os.path.join(self.tmp, "reports"), exist_ok=True)
        with open(os.path.join(self.tmp, "reports", "2026-10-20-policy-report.docx"), "wb") as f:
            f.write(b"x")
        with mock.patch.object(self.report, "DATA_DIR", self.tmp):
            with redirect_stdout(io.StringIO()) as buf:
                rc = self.report.ensure_docx("2026-10-20")
        self.assertEqual(rc, 0)
        self.assertIn("已存在", buf.getvalue())

    def test_ensure_docx_generates_when_missing(self):
        def fake_build(date, data=None, out_dir=None, db_path=None):
            out = os.path.join(out_dir, "%s-policy-report.docx" % date)
            os.makedirs(out_dir, exist_ok=True)
            with open(out, "wb") as f:
                f.write(b"docx")
            return out
        # Word 是 Markdown 的同源产物: md 在才补 docx(md 都没有 → 补出来是空壳)
        os.makedirs(os.path.join(self.tmp, "reports"), exist_ok=True)
        with open(os.path.join(self.tmp, "reports", "2026-10-20-policy-report.md"), "w",
                  encoding="utf-8") as f:
            f.write("# 日报")
        with mock.patch.object(self.report, "DATA_DIR", self.tmp), \
             mock.patch.dict(sys.modules, {"report_docx": mock.Mock(build=fake_build)}):
            with redirect_stdout(io.StringIO()) as buf:
                rc = self.report.ensure_docx("2026-10-20")
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertTrue(os.path.isfile(
            os.path.join(self.tmp, "reports", "2026-10-20-policy-report.docx")))

    def test_ensure_docx_not_generated_without_md(self):
        # 整份日报都没生成(检索全废)时补 Word = 造空壳掩盖失败(10-22 实测:
        # 兜底生成了 62KB 的空 Word, 看着像"报告已出", 实际当天 0 条记录)
        with mock.patch.object(self.report, "DATA_DIR", self.tmp):
            with redirect_stdout(io.StringIO()) as buf:
                rc = self.report.ensure_docx("2026-10-22")
        self.assertEqual(rc, 1)
        self.assertIn("不补 Word", buf.getvalue())
        self.assertFalse(os.path.exists(os.path.join(
            self.tmp, "reports", "2026-10-22-policy-report.docx")))

    def test_ensure_docx_reports_failure(self):
        def boom(*a, **k):
            raise RuntimeError("docx 炸了")
        with mock.patch.object(self.report, "DATA_DIR", self.tmp), \
             mock.patch.dict(sys.modules, {"report_docx": mock.Mock(build=boom)}):
            with redirect_stdout(io.StringIO()) as buf:
                rc = self.report.ensure_docx("2026-10-20")
        self.assertEqual(rc, 1)
        self.assertIn("[WARN]", buf.getvalue())

    def test_ensure_docx_handles_systemexit(self):
        # report_docx 在"政策库为空"时抛的是 SystemExit(BaseException 而非
        # Exception), 不接住会直接打断推送流程 —— 必须降级成 WARN 而不是崩。
        def boom(*a, **k):
            raise SystemExit("政策库为空")
        with mock.patch.object(self.report, "DATA_DIR", self.tmp), \
             mock.patch.dict(sys.modules, {"report_docx": mock.Mock(build=boom)}):
            with redirect_stdout(io.StringIO()) as buf:
                rc = self.report.ensure_docx("2026-10-20")
        self.assertEqual(rc, 1)
        self.assertIn("[WARN]", buf.getvalue())
        self.assertNotIn("Traceback", buf.getvalue())

    # ---- push_report 侧 ----

    def _run_push(self, args):
        sent = []

        # 2026-10-25 起 Word 按需生成, 推送不得自动补 —— 用哨兵桩证明 ensure_docx
        # 没被调用过(被调用则 sent 里出现 ("ensure", ...) 元组)
        def fake_ensure(date, db_path=None):
            sent.append(("ensure", date))
            return 0

        buf = io.StringIO()
        with mock.patch.object(self.pr, "PUSH_FLAG_DIR", os.path.join(self.tmp, ".pushed")), \
             mock.patch.object(self.pr, "PENDING_DIR", os.path.join(self.tmp, ".pending")), \
             mock.patch.object(self.ps, "SCOPE_DIR", os.path.join(self.tmp, ".scope")), \
             mock.patch.object(self.pr, "send_dingtalk",
                               side_effect=lambda t, title: sent.append(("dingtalk", t, title))), \
             mock.patch.object(self.pr.report, "collect",
                               return_value={"new": []}), \
             mock.patch.object(self.pr.report, "ensure_docx", side_effect=fake_ensure), \
             mock.patch("sys.argv", ["push_report.py"] + args), \
             redirect_stdout(buf):
            try:
                rc = self.pr.main()
            except SystemExit as e:
                rc = e.code
        return rc, buf.getvalue(), sent

    def test_push_report_does_not_regenerate_docx(self):
        # 2026-10-25 调整: Excel/Word 按需人工生成, 推送**不得**自动补 ——
        # Word 缺失时只发正文并如实标注, 不调用 report.ensure_docx
        rc, out, sent = self._run_push(["--date", "2026-12-10", "--review-channel", "dingtalk"])
        self.assertEqual(rc, 0, out)
        self.assertNotIn(("ensure", "2026-12-10"), sent)

    def test_push_report_marks_missing_word_in_body(self):
        # Word 缺失时, 正文必须如实标注"无 Word 附件", 不能假装有
        rc, out, sent = self._run_push(["--date", "2026-12-11", "--review-channel", "dingtalk"])
        self.assertEqual(rc, 0, out)
        bodies = [c[1] for c in sent if c[0] == "dingtalk"]
        self.assertEqual(len(bodies), 1, out)
        self.assertIn("无 Word 附件", bodies[0])

    def test_brief_text_marks_missing_word(self):
        data = {"new": []}
        self.assertIn("无 Word 附件", self.pr.brief_text("2026-12-12", data, docx_ok=False))
        self.assertNotIn("无 Word 附件", self.pr.brief_text("2026-12-12", data, docx_ok=True))

    def test_send_hermes_warns_when_docx_missing(self):
        rec = {}

        def fake_run(cmd, **kw):
            rec["input"] = kw.get("input")

            class P:
                returncode = 0
                stdout = b"ok"
                stderr = b""
            return P()

        missing = os.path.join(self.tmp, "不存在.docx")
        with mock.patch.object(self.pr.subprocess, "run", fake_run), \
                redirect_stdout(io.StringIO()) as buf:
            self.pr.send_hermes("正文", "weixin", missing)
        self.assertIn("附件缺失", buf.getvalue())
        self.assertNotIn("MEDIA:", rec["input"].decode("utf-8"))


class ScanFailureGuardTests(unittest.TestCase):
    """扫描失败的守门(2026-10-22 生产现场)。

    10-22 实测: 检索阶段卡死 44 分钟, Agent 自行放弃, **rc=0 假成功** ——
    一条没入库、一份日报没出, 监控完全看不出来; 而 cron 早上留下的 broadcast 锁
    还留在盘上, 当天任何时候 `push_report.py --broadcast` 都会被它放行 → 群发到
    钉钉(**10-20 事故的重演路径**, 且 cron 上下文校验拦不住: 锁是 cron 写的)。
    """

    def setUp(self):
        import push_report
        import push_scope
        import report
        self.pr = push_report
        self.ps = push_scope
        self.report = report
        self.tmp = tempfile.mkdtemp(prefix="scan-guard-")
        self.env_backup = dict(os.environ)
        os.environ["DINGTALK_WEBHOOK"] = "https://test.example/dingtalk"
        os.environ.pop("REVIEW_CHANNEL", None)
        self.sent = []
        self._patches = [
            mock.patch.object(self.pr, "PUSH_FLAG_DIR", os.path.join(self.tmp, ".pushed")),
            mock.patch.object(self.pr, "PENDING_DIR", os.path.join(self.tmp, ".pending")),
            mock.patch.object(self.ps, "SCOPE_DIR", os.path.join(self.tmp, ".scope")),
            mock.patch.object(self.pr, "send_dingtalk",
                              side_effect=lambda t, title: self.sent.append(("dingtalk", t))),
            mock.patch.object(self.pr.report, "collect",
                              return_value={"new": []}),
            mock.patch.object(self.pr.report, "ensure_docx", return_value=0),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        os.environ.clear()
        os.environ.update(self.env_backup)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_lock(self, date, scope, declared_at, source="run_scan.sh:policy"):
        """直接写锁文件(可以指定 declared_at, 用来造"过期残锁")。"""
        os.makedirs(self.ps.SCOPE_DIR, exist_ok=True)
        with open(self.ps.scope_path(date), "w", encoding="utf-8") as f:
            f.write("scope=%s\nsource=%s\ndeclared_at=%s\n" % (scope, source, declared_at))

    def _write_lock_aged(self, date, scope, minutes):
        """按 biz_now() 回拨 N 分钟造锁 —— 不依赖真实时刻, 测试稳定。"""
        dt = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)
              - datetime.timedelta(minutes=minutes))
        self._write_lock(date, scope, dt.strftime("%Y-%m-%dT%H:%M:%S"))

    def _run_push(self, args):
        buf = io.StringIO()
        with mock.patch("sys.argv", ["push_report.py"] + args), \
                redirect_stdout(buf):
            try:
                rc = self.pr.main()
            except SystemExit as e:
                rc = e.code
        return rc, buf.getvalue()

    # ---- 残留广播锁(红线) ----

    def test_stale_broadcast_lock_is_refused(self):
        # cron 早上声明、那一轮其实没跑完 → 锁留在盘上; 它不得放行全量推送
        self._write_lock_aged("2026-10-22", "broadcast", 180)
        rc, out = self._run_push(["--date", "2026-10-22", "--broadcast"])
        self.assertNotEqual(rc, 0, out)
        self.assertEqual(self.sent, [], out)
        self.assertIn("过期", out)

    def test_lock_within_window_is_allowed(self):
        # "这一轮还在进行"的合法窗口内, 锁仍要放行(否则定时任务自己会被卡住)
        self._write_lock_aged("2026-10-22", "broadcast", 5)
        rc, out = self._run_push(["--date", "2026-10-22", "--broadcast"])
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.sent, out)

    def test_fresh_broadcast_lock_is_allowed(self):
        self.ps.write_scope("2026-10-22", "broadcast", source="unit")
        rc, out = self._run_push(["--date", "2026-10-22", "--broadcast"])
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.sent, out)

    def test_scope_age_and_stale(self):
        from normalize import biz_now
        fresh = {"declared_at": biz_now()}
        self.assertLess(self.ps.scope_age_minutes(fresh), 1)
        self.assertFalse(self.ps.scope_is_stale(fresh))
        # 解析不出声明时间的旧格式锁不判过期(不因加校验把历史锁全作废)
        self.assertIsNone(self.ps.scope_age_minutes({"scope": "broadcast"}))
        self.assertFalse(self.ps.scope_is_stale({"scope": "broadcast"}))

    # ---- 编排层静态回归 ----

    def _run_scan_text(self):
        with open(os.path.join(REPO, "scripts", "run_scan.sh"), encoding="utf-8") as f:
            return f.read()

    def test_run_scan_validates_artifacts(self):
        # 退出码不能信(Agent 放弃时 rc=0) → 必须核对产物, 且产物得是这一轮新生成的
        text = self._run_scan_text()
        self.assertIn("产物校验", text)
        self.assertIn("_fresh", text)
        self.assertIn("本轮扫描未完成", text)

    def test_run_scan_has_timeout_watchdog(self):
        # 10-22 白烧 44 分钟 → 跑批必须有硬上限
        text = self._run_scan_text()
        self.assertIn("SCAN_TIMEOUT", text)
        self.assertIn("timeout -k 30", text)

    def test_run_scan_prechecks_gateway(self):
        # 10-20 实测: 撞上网关重启(Gateway draining)时整轮扫描直接废掉
        text = self._run_scan_text()
        self.assertIn("HERMES_HEALTH_CMD", text)
        self.assertIn("网关健康预检", text)

    def test_skill_has_tool_discipline(self):
        # 10-22 的直接教训: 取数要有降级顺序与搜索预算, 否则 50 次打转到撞上限
        with open(os.path.join(REPO, "skills", "global-policy-search", "SKILL.md"),
                  encoding="utf-8") as f:
            text = f.read()
        self.assertIn("取数工具纪律", text)
        self.assertIn("失败 2 次就换", text)
        self.assertIn("搜索预算", text)
        self.assertIn("collect.py", text)

    def test_skill_pins_eping_to_browse_path(self):
        # 10-22 实测: Agent 自己撞了 ePing 的 API, 拉回 142KB 不带筛选的全量包,
        # 再逐条下 PDF 核日期(23+ 个文件 / 40 分钟没完)。skill 必须把路径钉死:
        # 只 browse 列表页、窗口外不下载、单轮有硬上限。
        with open(os.path.join(REPO, "skills", "global-policy-search", "SKILL.md"),
                  encoding="utf-8") as f:
            text = f.read()
        self.assertIn("固定走「列表页」", text)
        self.assertIn("不许拉 API 包", text)
        self.assertIn("下载 PDF ≤ 6", text)

    def test_collect_enforces_daily_archive_cap(self):
        # 存档量上限必须是**代码强制**(不依赖 Agent 自律), 且在抓取之前就拒绝
        # 2026-10-27(A1): 上限口径 = **存档条数**(数 *.meta.json)。原实现数的是
        # 目录文件总数, 而每条存档产出两个文件(正文 + .meta.json), 再叠加取数段的
        # 交付文件, 于是"上限 12"实际只存得下 6 条 —— 而全链路其余环节
        # (fetch_pages 的 already / run_scan.sh 的 RAW_N 终检)都按条数计,
        # 两套口径不一致, 每日覆盖被悄悄压掉一半。
        import collect
        raw_dir = os.path.join(self.tmp, "raw", "2026-10-22")
        os.makedirs(raw_dir, exist_ok=True)
        for i in range(12):
            with open(os.path.join(raw_dir, "%02d-x.meta.json" % i), "w", encoding="utf-8") as f:
                f.write("{}")
        buf = io.StringIO()
        with mock.patch.object(collect, "DATA_DIR", self.tmp), \
             mock.patch("sys.argv", ["collect.py", "--url", "https://example.com/1",
                                     "--title", "测试", "--date", "2026-10-22"]), \
             redirect_stdout(buf):
            rc = collect.main()
        self.assertEqual(rc, 1)
        self.assertIn("[拒绝]", buf.getvalue())
        self.assertIn("已有 12 条存档", buf.getvalue())
        # 触顶后不应新增文件
        self.assertEqual(len(os.listdir(raw_dir)), 12)

    def test_collect_delivery_files_do_not_consume_archive_quota(self):
        # A1 的另一半: 交付文件(candidates.jsonl / search-log.md /
        # extracted-policy.jsonl / .fetch-done)是取数段的过程产物, **不占存档配额**。
        # 修复前它们被计入目录文件总数, 把 12 条的配额吃到只剩 6 条。
        import collect
        raw_dir = os.path.join(self.tmp, "raw", "2026-10-23")
        os.makedirs(raw_dir, exist_ok=True)
        for i in range(11):
            with open(os.path.join(raw_dir, "%02d-x.meta.json" % i), "w", encoding="utf-8") as f:
                f.write("{}")
        for name in ("candidates.jsonl", "search-log.md", "extracted-policy.jsonl", ".fetch-done"):
            with open(os.path.join(raw_dir, name), "w", encoding="utf-8") as f:
                f.write("x")
        body = os.path.join(self.tmp, "body-a.txt")
        with open(body, "w", encoding="utf-8") as f:
            f.write("正文")
        buf = io.StringIO()
        with mock.patch.object(collect, "DATA_DIR", self.tmp), \
             mock.patch("sys.argv", ["collect.py", "--url", "https://example.com/1",
                                     "--file", body, "--title", "测试",
                                     "--date", "2026-10-23"]), \
             redirect_stdout(buf):
            rc = collect.main()
        self.assertEqual(rc, 0)
        self.assertEqual(collect.count_archives(raw_dir), 12)

    def test_collect_archive_cap_param_alias(self):
        # 2026-10-27: 上限参数改名 --max-archives-per-day(语义=存档条数),
        # 旧名 --max-files-per-day 保留为别名; 两个都给且不一致 → 报错退出
        # (上限口径的歧义会让"今天能存几条"变成猜)。
        import collect
        raw_dir = os.path.join(self.tmp, "raw", "2026-10-24")
        os.makedirs(raw_dir, exist_ok=True)
        body = os.path.join(self.tmp, "body-b.txt")
        with open(body, "w", encoding="utf-8") as f:
            f.write("正文")
        # 旧名仍可用
        buf = io.StringIO()
        with mock.patch.object(collect, "DATA_DIR", self.tmp), \
             mock.patch("sys.argv", ["collect.py", "--url", "https://example.com/1",
                                     "--file", body, "--title", "旧名",
                                     "--date", "2026-10-24",
                                     "--max-files-per-day", "5"]), \
             redirect_stdout(buf):
            rc = collect.main()
        self.assertEqual(rc, 0)
        self.assertEqual(collect.count_archives(raw_dir), 1)
        # 新旧名冲突 → 拒绝(不猜)
        buf2 = io.StringIO()
        with mock.patch.object(collect, "DATA_DIR", self.tmp), \
             mock.patch("sys.argv", ["collect.py", "--url", "https://example.com/2",
                                     "--file", body, "--title", "冲突",
                                     "--date", "2026-10-24",
                                     "--max-archives-per-day", "5",
                                     "--max-files-per-day", "9"]), \
             redirect_stdout(buf2):
            rc = collect.main()
        self.assertEqual(rc, 2)
        self.assertIn("不一致", buf2.getvalue())

    def test_run_scan_checks_archive_volume(self):
        # 终检: 绕过 collect.py 直接往 raw 目录倾倒也要被识破
        text = self._run_scan_text()
        self.assertIn("存档量超限", text)

    def test_run_scan_counts_meta_files_only(self):
        # 2026-10-24 体检 H2: 原先 RAW_N 数"所有非 meta 文件", 把取数段强制创建的
        # 交付文件(candidates*/extracted-policy*/search-log.md, prompt 要求
        # 哪怕 0 条也要建)全数进去 —— 安静的一天 RAW_N 也 ≥3, "当日有存档却 0 入库"
        # 恒触发, 每天必红。必须只数 *.meta.json(collect.py 每条存档恰一个)。
        text = self._run_scan_text()
        self.assertIn("-name '*.meta.json'", text)
        self.assertNotIn("! -name '*.meta.json'", text)

    def test_run_scan_trap_armed_before_scope_case(self):
        # 2026-10-24 体检 H1: _BROADCAST_ARMED=0 的初始化原先写在 case 块**之后**,
        # 把 broadcast 分支刚设的 1 无条件清零 —— EXIT trap 撤锁在所有退出路径上
        # 空转, "任何非正常退出一律撤销"的保护形同虚设。初始化必须在 case 之前。
        text = self._run_scan_text()
        init = text.index("_BROADCAST_ARMED=0")
        arm = text.index("_BROADCAST_ARMED=1")
        case_start = text.index('case "$SCOPE" in')
        self.assertLess(init, case_start,
                        "布防初始化必须出现在范围声明 case 之前")
        self.assertLess(case_start, arm,
                        "布防置 1 必须发生在范围声明 case 内(锁写下之后)")


class TokenEconomyTests(unittest.TestCase):
    """取数段 token 经济(2026-10-24 二次修复)。

    f7ae1c4 前单轮流程 ~65k/轮; 拆段+纪律后实测 200-250k/轮。差距经 state.db
    实证定位在流程复杂度反噬: ①Agent 临场 read_file 58KB AGENTS.md/16KB SKILL.md;
    ②python 读改写整个 JSON(每追加一条读回一次)+打印整条记录"验证"(单次 11KB);
    ③prompt 越写越长。对应三味药: 当日清单预渲染注入 / JSONL 追加且不打印 /
    严禁读仓库文件。
    """

    def test_run_scan_prompt_uses_jsonl_and_bans_fileread(self):
        # prompt 必须已切换到 JSONL 追加 + 禁读仓库文件 + 禁验证打印
        # (2026-10-24 P2: 追加改 jsonl_append.py 硬约束, 断言随行为更新)
        with open(os.path.join(REPO, "scripts", "run_scan.sh"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("extracted-policy.jsonl", text)
        self.assertIn("candidates.jsonl", text)
        self.assertIn("严禁 read_file", text)
        self.assertIn("jsonl_append.py", text)
        self.assertIn("不打印记录内容", text)
        self.assertIn("scan_targets.py", text)          # 当日清单预渲染注入
        self.assertIn("COUNTRY_BLOCK", text)
        # 旧指令不得残留: "追加写入 ... candidates.json"(读改写整个 JSON 的病根)
        self.assertNotIn("追加写入 data/raw/$SCAN_DATE/candidates.json", text)

    def test_scan_targets_daily_full_scan(self):
        # 2026-10-25 起每日全扫(P4 零 token 取数后周轮转废弃): 任意两天清单一致,
        # 六档全在, 轮转档源(俄/英/日等)不再有"无调度日"。防 10-24 配置断裂回归
        # (当时 border_neighbours/export_partners 23 个源没有任何调度日, H17)。
        import scan_targets
        import datetime
        days = [datetime.date(2026, 10, d).isoformat() for d in range(24, 31)]
        lists = [[(t, sid) for t, _cn, _en, sid, _u in scan_targets._pick(d)] for d in days]
        first = lists[0]
        for d, lst in zip(days[1:], lists[1:]):
            self.assertEqual(lst, first, d)  # 每日清单完全一致
        tiers_seen = {t for t, _ in first}
        for tier in ("priority_1", "regional", "priority_2",
                     "border_neighbours", "export_partners", "state_level"):
            self.assertIn(tier, tiers_seen, "档位缺失: %s" % tier)
        sids = {sid for _t, sid in first}
        for sid in ("ru-fsvps", "uk-defra", "jp-maff", "vn-mard", "au-agriculture"):
            self.assertIn(sid, sids, "轮转档源失明: %s" % sid)
        # 同源跨档重复挂载只扫一次
        self.assertEqual(len(sids), len(first))
        self.assertGreaterEqual(len(first), 40)  # 47 policy 源的量级下限
        # 7 天并集 = 每日清单(全扫不变量)
        union = set(sids)
        for lst in lists[1:]:
            union |= {sid for _t, sid in lst}
        self.assertEqual(union, sids)

    def test_scan_targets_injects_source_urls(self):
        # 清单必须直出源入口 URL, Agent 才不需要读 sources.yaml
        import scan_targets
        out, _ = scan_targets.render("2026-10-27")
        self.assertRegex(out, r"ca-cfia https?://\S+")
        self.assertRegex(out, r"wto-eping https?://\S+")

    def test_synth_candidates_writes_jsonl_and_merges_legacy(self):
        # synth 兜底: 输出 candidates.jsonl; 合并已有 jsonl + 旧数组版 candidates.json;
        # 按 url 去重; Agent 已写的语义字段不被覆盖
        import synth_candidates
        tmp = tempfile.mkdtemp(prefix="synth-jsonl-")
        raw_dir = os.path.join(tmp, "raw", "2026-10-24")
        os.makedirs(raw_dir)
        for i, (title, url) in enumerate([
                ("甲政策", "https://official.example/a"),
                ("乙政策", "https://official.example/b")], 1):
            with open(os.path.join(raw_dir, "%02d-x.meta.json" % i), "w",
                      encoding="utf-8") as f:
                json.dump({"title": title, "url": url}, f, ensure_ascii=False)
        # 已有 jsonl 一条(url=A, 带语义字段 country_en 已填) + 旧数组版一条(url=C)
        with open(os.path.join(raw_dir, "candidates.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"title": "甲政策", "url": "https://official.example/a",
                                "country_en": "Demo Land"}, ensure_ascii=False) + "\n")
        with open(os.path.join(raw_dir, "candidates.json"), "w", encoding="utf-8") as f:
            json.dump([{"title": "丙政策", "url": "https://official.example/c"}], f,
                      ensure_ascii=False)
        try:
            with mock.patch.object(synth_candidates, "DATA_DIR", tmp), \
                    mock.patch("sys.argv", ["synth_candidates.py",
                                            "--date", "2026-10-24"]):
                rc = synth_candidates.main()
            self.assertEqual(rc, 0)
            with open(os.path.join(raw_dir, "candidates.jsonl"), encoding="utf-8") as f:
                lines = [json.loads(x) for x in f.read().splitlines() if x.strip()]
            urls = [x["url"] for x in lines]
            # 3 个不同 url 各一行, 无重复; A 的语义字段保留, B/C 由 meta/旧文件补齐
            self.assertEqual(sorted(urls), sorted(set(urls)))
            self.assertEqual(len(lines), 3)
            a = next(x for x in lines if x["url"].endswith("/a"))
            self.assertEqual(a["country_en"], "Demo Land")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_normalize_loads_jsonl_with_bad_lines(self):
        # JSONL 输入: 好行入库、坏行(写入中断的半行)跳过并 WARN、空行忽略
        from normalize import _load_input
        tmp = tempfile.mkdtemp(prefix="norm-jsonl-")
        try:
            p = os.path.join(tmp, "x.jsonl")
            with open(p, "w", encoding="utf-8") as f:
                f.write('{"a": 1}\n\n{"a": 2}\n{"a": trunc')   # 末行是半条
            with mock.patch("sys.stdout", new_callable=io.StringIO):
                records = _load_input(p)
            self.assertEqual(records, [{"a": 1}, {"a": 2}])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TokenBudgetBrakeTests(unittest.TestCase):
    """取数幂等锁 + 日预算刹车(2026-10-24 token 爆量事故)。

    当天 6 个扫描会话烧穿 Token Plan(429, 连微信对话都调不动); 6 条存档
    09:39 已落齐, 之后 12:50/13:54/14:55 三轮重取(其中 4 个会话绕过
    run_scan.sh 直接 hermes -z, 无超时无幂等检查)零新增、白烧 ~126 万输入
    token + 3200 万 cache 读。对应三道闸: .fetch-done 幂等锁 / token_budget.py
    预算刹车 / run_scan.sh --stage fetch 统一发射口。
    """

    def _make_db(self, path, rows):
        import sqlite3
        con = sqlite3.connect(path)
        con.execute(
            "CREATE TABLE sessions (id TEXT, started_at REAL, input_tokens INTEGER,"
            " output_tokens INTEGER, cache_read_tokens INTEGER, title TEXT)")
        con.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?)", rows)
        con.commit()
        con.close()

    def _rows_for(self, date):
        """两条当日会话 + 一条前一日深夜会话(不应计入)。"""
        import token_budget as tb
        start, _ = tb.day_window(date)
        return [
            ("s1", start + 3600, 100000, 50000, 1000000, "取数会话A"),
            ("s2", start + 7200, 200000, 50000, 2000000, "取数会话B"),
            ("s3", start - 10, 999999, 99999, 9999999, "前一日深夜"),
        ]

    def _run_budget(self, args):
        import token_budget
        buf = io.StringIO()
        with mock.patch("sys.argv", ["token_budget.py"] + args), redirect_stdout(buf):
            try:
                rc = token_budget.main()
            except SystemExit as e:
                rc = e.code
        return rc, buf.getvalue()

    def test_over_budget_returns_3(self):
        # 当日折合 30万输入+10万输出+300万cache*0.1=70万 → 预算 60万时必须拦
        tmp = tempfile.mkdtemp(prefix="budget-over-")
        db = os.path.join(tmp, "state.db")
        self._make_db(db, self._rows_for("2026-10-24"))
        try:
            rc, out = self._run_budget(["--date", "2026-10-24", "--db", db,
                                        "--budget", "600000"])
            self.assertEqual(rc, 3, out)
            self.assertIn("已达预算上限", out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_under_budget_returns_0_and_excludes_other_days(self):
        # 预算内 rc=0; 且窗口是东八区当日, 前一日深夜的会话不计入(会话数=2)
        tmp = tempfile.mkdtemp(prefix="budget-under-")
        db = os.path.join(tmp, "state.db")
        self._make_db(db, self._rows_for("2026-10-24"))
        try:
            rc, out = self._run_budget(["--date", "2026-10-24", "--db", db,
                                        "--budget", "800000"])
            self.assertEqual(rc, 0, out)
            self.assertIn("预算内", out)
            self.assertIn("会话 2 个", out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_missing_db_fails_open(self):
        # fail-open: 测不到(本机无 state.db)就不拦跑批, 但要大声说
        rc, out = self._run_budget(["--date", "2026-10-24",
                                    "--db", os.path.join(tempfile.gettempdir(), "no-such-x.db")])
        self.assertEqual(rc, 0, out)
        self.assertIn("WARN", out)

    def _run_scan_text(self):
        with open(os.path.join(REPO, "scripts", "run_scan.sh"), encoding="utf-8") as f:
            return f.read()

    def test_run_scan_has_fetch_idempotency_lock(self):
        # 取数幂等锁: 当日已交付(.fetch-done)必须跳过重取; 强制重取必须显式 --refetch
        text = self._run_scan_text()
        self.assertIn(".fetch-done", text)
        self.assertIn("--refetch", text)
        self.assertIn("FETCH_DONE", text)
        self.assertIn("跳过取数段", text)

    def test_run_scan_has_budget_brake(self):
        # 预算刹车: 取数段启动前必须查当日消耗, 超限降级为只加工
        text = self._run_scan_text()
        self.assertIn("token_budget.py", text)
        self.assertIn("预算检查", text)
        self.assertIn("FETCH_SKIP", text)

    def test_run_scan_stage_fetch_exits_before_process(self):
        # --stage fetch 只取数不加工: 完成即退出, 不得进入加工段/产物校验
        text = self._run_scan_text()
        self.assertIn("--stage fetch", text)
        exit_pos = text.index("run_scan --stage fetch 完成")
        process_pos = text.index("阶段 2/2 加工出报")
        self.assertLess(exit_pos, process_pos,
                        "--stage fetch 的收尾退出必须早于加工段启动")


class FetchGroupingTests(unittest.TestCase):
    """取数分块 + 通道确定性预探(2026-10-24 P1, 10-25 晨跑再修)。

    10-25 晨跑实证: 必扫 21 源对单组 50 轮仍打满, 且 Agent 把候选囤在对话里
    0 交付 → 按 GROUP_CHUNK=6 源对/块切, 每块 ≈30 轮内收敛; HTTP 探测前移出
    Agent(治理后一轮仍有 26 terminal + 25 execute_code 的探测/摘录调用)。
    """

    def test_scan_targets_chunking_invariants(self):
        # 不变量: 块号连续从1; 每块 ≤ GROUP_CHUNK; 各块之和 = 全量; 块K = 全量按序的第 K 段
        import scan_targets
        for date in ("2026-10-25", "2026-10-26", "2026-10-27"):
            info = scan_targets.groups_info(date)
            self.assertEqual(sorted(info), list(range(1, len(info) + 1)), date)
            self.assertTrue(all(0 < v <= scan_targets.GROUP_CHUNK for v in info.values()), date)
            full = scan_targets._pick(date)
            self.assertEqual(sum(info.values()), len(full), date)
            for k in info:
                self.assertEqual(scan_targets._pick(date, k),
                                 full[(k - 1) * scan_targets.GROUP_CHUNK:
                                      k * scan_targets.GROUP_CHUNK], date)
        # 每日全扫(2026-10-25): 47 源(跨档重复挂载去重) → 8 块(6×7+5, 钉死防回归)
        self.assertEqual(scan_targets.groups_info("2026-10-25"),
                         {1: 6, 2: 6, 3: 6, 4: 6, 5: 6, 6: 6, 7: 6, 8: 5})

    def test_scan_targets_groups_cli_machine_readable(self):
        # run_scan.sh 逐 token 解析 n1=.. nK=.. —— 格式钉死防漂移(块数可变)
        import scan_targets
        out = io.StringIO()
        with mock.patch("sys.argv", ["scan_targets.py", "--date", "2026-10-25",
                                     "--groups"]), redirect_stdout(out):
            rc = scan_targets.main()
        self.assertEqual(rc, 0)
        self.assertRegex(out.getvalue(), r"^n1=[0-9]+( n[0-9]+=[0-9]+)*\n$")
        info = scan_targets.groups_info("2026-10-25")
        self.assertIn(" ".join("n%d=%d" % kv for kv in sorted(info.items())),
                      out.getvalue())

    def test_probe_channels_classify_and_clean(self):
        # ok/dead 口径与 channel_health 的「不可用」一致; 输出会被拼进 prompt, 必须洗净
        import probe_channels as pc
        self.assertEqual(pc.classify(200), "ok")
        self.assertEqual(pc.classify(302), "ok")
        self.assertEqual(pc.classify(403), "dead")
        self.assertEqual(pc.classify(500), "dead")
        self.assertEqual(pc.classify(0), "dead")     # 连接失败/超时
        cleaned = pc._clean("bad \"quote\" `bt` $dollar")
        self.assertNotIn("\"", cleaned)
        self.assertNotIn("`", cleaned)
        self.assertNotIn("$", cleaned)

    def test_http_get_fallback_chain(self):
        # 2026-10-25 死源分诊修复(20死源中7个可复活): --http1.1 常驻 / SSL链错误
        # 加 -k 重试 / 403 去 UA 重试 / 5xx 加长超时重试。桩 subprocess.run 逐层验证。
        import probe_channels as pc

        def resp(code, returncode=0):
            return mock.Mock(returncode=returncode, stdout="\n%d" % code, stderr="")

        def resp_fail(returncode):
            # 真实 curl 在连接/SSL 阶段死亡: rc!=0 但 -w 仍输出 000(stdout 非空)
            return mock.Mock(returncode=returncode, stdout="\n000", stderr="curl: (60) SSL")

        calls = []
        with mock.patch.object(pc.subprocess, "run",
                               side_effect=lambda cmd, **k: (calls.append(cmd), resp(200))[1]):
            code, _body, note = pc.http_get("https://x/", 5)
        self.assertEqual((code, note), (200, ""))
        self.assertIn("--http1.1", calls[0])
        self.assertIn("-A", calls[0])

        # SSL 链错误(curl 退出 60) → 第二次带 -k 重试成功
        calls = []
        def ssl_then_ok(cmd, **k):
            calls.append(cmd)
            return resp_fail(60) if len(calls) == 1 else resp(200)
        with mock.patch.object(pc.subprocess, "run", side_effect=ssl_then_ok):
            code, _body, note = pc.http_get("https://x/", 5)
        self.assertEqual(code, 200)
        self.assertEqual(note, "ssl-retry")
        self.assertNotIn("-k", calls[0])
        self.assertIn("-k", calls[1])

        # 403 → 第二次不带 -A(无 UA)重试成功(aphis.usda.gov 型 WAF)
        calls = []
        def ua403_then_ok(cmd, **k):
            calls.append(cmd)
            return resp(403) if len(calls) == 1 else resp(200)
        with mock.patch.object(pc.subprocess, "run", side_effect=ua403_then_ok):
            code, _body, note = pc.http_get("https://x/", 5)
        self.assertEqual(code, 200)
        self.assertEqual(note, "no-ua-retry")
        self.assertIn("-A", calls[0])
        self.assertNotIn("-A", calls[1])

        # 5xx → 加长超时重试成功(co-minagricultura 型慢站)
        calls = []
        def slow504_then_ok(cmd, **k):
            calls.append(cmd)
            return resp(504) if len(calls) == 1 else resp(200)
        with mock.patch.object(pc.subprocess, "run", side_effect=slow504_then_ok):
            code, _body, note = pc.http_get("https://x/", 6)
        self.assertEqual(code, 200)
        self.assertEqual(note, "slow-retry")
        self.assertEqual(calls[1][calls[1].index("-m") + 1], "18")  # 6*3

        # 超时 → -k + 超时三倍重试(握手慢 + 证书链不完整的复合形态, ca-cfia 型)
        calls = []
        def timeout_then_ok(cmd, **k):
            calls.append(cmd)
            if len(calls) == 1:
                raise pc.subprocess.TimeoutExpired(cmd, 6)
            return resp(200)
        with mock.patch.object(pc.subprocess, "run", side_effect=timeout_then_ok):
            code, _body, note = pc.http_get("https://x/", 6)
        self.assertEqual(code, 200)
        self.assertEqual(note, "ssl-retry")
        self.assertIn("-k", calls[1])
        self.assertEqual(calls[1][calls[1].index("-m") + 1], "18")  # 6*3

        # 全部失败: 保持死判, 不抛异常
        with mock.patch.object(pc.subprocess, "run", side_effect=lambda *a, **k: resp_fail(60)):
            code, _body, _note = pc.http_get("https://x/", 5)
        self.assertEqual(code, 0)

    def test_rufsvps_entry_url_fixed(self):
        # B类修复: 旧路径 /ru/vse-soobshcheniya 已 404, 入口改站点根(200)
        import scan_targets
        for _t, _cn, _en, sid, url in scan_targets._pick("2026-10-25"):
            if sid == "ru-fsvps":
                self.assertEqual(url, "https://fsvps.gov.ru/")
                break
        else:
            self.fail("ru-fsvps 不在当日清单")

    def _run_scan_text(self):
        with open(os.path.join(REPO, "scripts", "run_scan.sh"), encoding="utf-8") as f:
            return f.read()

    def test_run_scan_fetch_loop_over_groups(self):
        # 分块循环: 变长块解析 + 模板占位符逐块替换 + 探测前移 + 加权超时
        text = self._run_scan_text()
        self.assertIn("@@COUNTRY_BLOCK@@", text)
        self.assertIn('"$REPO/scripts/scan_targets.py" --date "$SCAN_DATE" --groups', text)
        self.assertIn("for _kv in $GROUPS_INFO", text)     # 变长 nK=.. 解析(10-25 改分块)
        self.assertIn("probe_channels.py", text)
        self.assertIn("G_TIMEOUT", text)
        self.assertIn("块$G 取数超时", text)
        # 模板改名 PROMPT_FETCH_TPL 后, policy 分支条件若仍查旧变量名会静默
        # 退化成单轮流程(干跑实测抓到过) —— 钉死条件必须查模板变量
        cond = 'if [ "$MODE" = "policy" ] && [ -n "${PROMPT_FETCH_TPL:-}" ]; then'
        self.assertIn(cond, text)
        # token 经济纪律不得在重构中丢失; 10-25 教训: 囤积候选不落盘 → 进度自检
        self.assertIn("严禁 read_file", text)
        self.assertIn("extracted-policy.jsonl", text)
        self.assertIn("进度自检", text)

    def test_run_scan_requires_explicit_mode(self):
        # 无参数不得静默默认 am 起真实扫描(2026-10-24 现场手滑实测);
        # fail-closed: 忘了指定模式的后果是"没跑", 不是"跑了个没料到的模式"
        text = self._run_scan_text()
        self.assertIn("未指定模式", text)
        self.assertNotIn('MODE="${MODE:-am}"', text)

    def test_run_scan_empty_input_short_circuit(self):
        # 2026-10-25 晨跑事故: 空输入下加工 Agent 自发联网重做取数(194万折合白烧、
        # 50 轮打满、入库/出报没跑到) → 0 存档且无候选/抽取记录时不派 Agent,
        # 直接确定性出报 + 按范围锁确定性推送
        text = self._run_scan_text()
        self.assertIn("加工段不派 Agent", text)
        self.assertIn("直接确定性出报", text)
        self.assertIn('"$REPO/scripts/report.py" --date "$SCAN_DATE"', text)
        # 2026-10-25 起 Excel/Word 按需生成: 跑批所有出报调用/prompt 指令都不得
        # 带 --excel/--docx/--ensure-docx(注释里的按需生成指引除外)
        self.assertNotIn('--date "$SCAN_DATE" --excel', text)
        self.assertNotIn('--date "$SCAN_DATE" --ensure-docx', text)
        self.assertNotIn("--date $SCAN_DATE --excel", text)
        self.assertNotIn("--date $SCAN_DATE --ensure-docx", text)
        self.assertIn('"$REPO/scripts/push_report.py" --date "$SCAN_DATE" --broadcast', text)
        self.assertIn('"$REPO/scripts/push_report.py" --date "$SCAN_DATE" --review-channel "$SCOPE_CHANNEL"', text)
        # 三重计数护栏(文件缺失时 wc 不再抛 bash 报错行)
        self.assertIn('[ -f "$FETCH_DIR/candidates.jsonl" ]', text)
        self.assertIn('[ -f "$FETCH_DIR/extracted-policy.jsonl" ]', text)

    def test_run_scan_only_group_test_mode(self):
        # 小token测试开关: 只跑指定块 + 不写幂等标记(不挡正式取数) + 块间预算复查
        text = self._run_scan_text()
        self.assertIn("--only-group", text)
        self.assertIn('[ -z "$ONLY_GROUP" ]', text)      # 测试语义不写 .fetch-done
        self.assertIn("块间预算复查", text)               # 前一块烧超, 剩余块不再发起

    def test_run_scan_stage_timeouts_beat_hermes_env(self):
        # 2026-10-24 二级测试实测: STAGE_FETCH_TIMEOUT=600 被 ~/.hermes/.env 的
        # 1200 覆盖(与 10-22 HERMES_RUN_CMD 同类 bug) —— 阶段超时必须进
        # "调用方优先于 .env"的保护名单, 否则小规模测试根本收不紧超时
        text = self._run_scan_text()
        self.assertIn('_caller_fetch_to="${STAGE_FETCH_TIMEOUT-__unset__}"', text)
        self.assertIn('export STAGE_FETCH_TIMEOUT="$_caller_fetch_to"', text)
        self.assertIn('_caller_process_to="${STAGE_PROCESS_TIMEOUT-__unset__}"', text)
        self.assertIn('export STAGE_PROCESS_TIMEOUT="$_caller_process_to"', text)

    def test_scope_max_age_widened_for_groups(self):
        # 拆组后最坏路径 ~70 分钟(两组加权超时 + 加工 + 网关重启等待),
        # 60 分钟会把"这一轮还在跑"的合法广播锁误判过期 → 全量推送被自己拦死
        import push_scope
        self.assertEqual(push_scope.SCOPE_MAX_AGE_MINUTES, 75)


class ToolOutputCapTests(unittest.TestCase):
    """工具输出硬上限(2026-10-24 P2): 纪律约束不住的, 用脚本保证。

    实证(10-24 治理后一轮): 单次 execute_code 输出仍 13.4KB(prompt 红线 8KB),
    25 次调用累计 115KB —— 把已存档 HTML 的链接清单倒进上下文。jsonl_append.py
    只回执一行、snip.py 硬性截断, 模型想违反也违反不了。
    """

    def _run_cli(self, mod, argv):
        buf = io.StringIO()
        with mock.patch("sys.argv", argv), redirect_stdout(buf):
            try:
                rc = mod.main()
            except SystemExit as e:
                rc = e.code
        return rc, buf.getvalue()

    def test_jsonl_append_receipt_only(self):
        # 追加成功只回执一行; 嵌套键落 dict; 空值存 null; 计数正确
        import jsonl_append
        tmp = tempfile.mkdtemp(prefix="jla-")
        f = os.path.join(tmp, "raw", "2026-10-24", "candidates.jsonl")
        try:
            rc, out = self._run_cli(jsonl_append, [
                "jsonl_append.py", f,
                "title=甲政策", "url=https://official.example/a",
                "country_en=Demo Land", "brief=", "products=牛肉,乳制品"])
            self.assertEqual(rc, 0, out)
            self.assertEqual(len(out.strip().splitlines()), 1, out)
            self.assertIn("total 1", out)
            with open(f, encoding="utf-8") as fh:
                rec = json.loads(fh.read().splitlines()[0])
            self.assertEqual(rec["title"], "甲政策")
            self.assertIsNone(rec["brief"])
            self.assertEqual(rec["products"], ["牛肉", "乳制品"])
            # 第二条: 抽取记录口径(title_cn + source.*) + total 计数
            rc, out = self._run_cli(jsonl_append, [
                "jsonl_append.py", f,
                "title_cn=乙政策", "source.url=https://official.example/b",
                "source.tier=1", "source.quote=原文关键句"])
            self.assertEqual(rc, 0, out)
            self.assertIn("total 2", out)
            with open(f, encoding="utf-8") as fh:
                rec = json.loads(fh.read().splitlines()[1])
            self.assertEqual(rec["source"]["url"], "https://official.example/b")
            self.assertEqual(rec["source"]["tier"], "1")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_jsonl_append_rejects_incomplete(self):
        # 缺 title/url 拒绝追加 —— 残行不入库
        import jsonl_append
        tmp = tempfile.mkdtemp(prefix="jla-")
        f = os.path.join(tmp, "c.jsonl")
        try:
            rc, out = self._run_cli(jsonl_append, ["jsonl_append.py", f, "title=只有标题"])
            self.assertEqual(rc, 1)
            self.assertIn("拒绝", out)
            self.assertFalse(os.path.exists(f))
            rc, out = self._run_cli(jsonl_append, ["jsonl_append.py", f, "不是键值对"])
            self.assertEqual(rc, 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_snip_hard_byte_cap(self):
        # 10KB 文件无论默认还是 --grep, 回显(含截断标注)都被压在 ~bytes+标注 内
        import snip
        tmp = tempfile.mkdtemp(prefix="snip-")
        p = os.path.join(tmp, "arch.html")
        body = ("x" * 200 + "目标关键词" + "y" * 200) * 25   # ~10KB
        with open(p, "w", encoding="utf-8") as f:
            f.write(body)
        try:
            rc, out = self._run_cli(snip, ["snip.py", p])
            self.assertEqual(rc, 0)
            self.assertLessEqual(len(out.encode("utf-8")), 2048 + 120, out)
            self.assertIn("已截断", out)
            rc, out = self._run_cli(snip, ["snip.py", p, "--grep", "目标关键词"])
            self.assertEqual(rc, 0)
            self.assertIn("目标关键词", out)
            self.assertLessEqual(len(out.encode("utf-8")), 2048 + 120, out)
            rc, out = self._run_cli(snip, ["snip.py", p, "--grep", "不存在词"])
            self.assertEqual(rc, 0)
            self.assertIn("无匹配", out)
            rc, out = self._run_cli(snip, ["snip.py", os.path.join(tmp, "no.html")])
            self.assertEqual(rc, 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_run_scan_prompt_pins_helper_tools(self):
        # prompt 必须钉死 snip/jsonl_append 用法(否则 Agent 退回 execute_code 自由发挥)
        with open(os.path.join(REPO, "scripts", "run_scan.sh"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("snip.py <存档文件> --grep", text)
        self.assertIn("source.publish_date=<YYYY-MM-DD>", text)   # 抽取键值模板内联


class PipelineP4Tests(unittest.TestCase):
    """P4 确定性管线(2026-10-25 定稿, 用户约束: 全链路 ≤10 分钟)。

    Agent 会话对「轮次上限/Token 耗尽/阶段超时」任一死法都是整轮全损
    (10-25 三连败) → 脚本开车: fetch_pages 零LLM取数 + extract_records
    每份存档一次单轮微调用; 单份失败只废那一份。
    """

    def test_fetch_pages_date_parsing(self):
        # 多语种日期形态: ISO / 欧序 / 中文 / 英文月份
        import fetch_pages
        import datetime as dt
        self.assertEqual(fetch_pages.parse_date("发布 2026-10-22（JSON-LD）"), dt.date(2026, 10, 22))
        self.assertEqual(fetch_pages.parse_date("18/10/2026 NORMAS"), dt.date(2026, 10, 18))
        self.assertEqual(fetch_pages.parse_date("2026年10月18日"), dt.date(2026, 10, 18))
        self.assertEqual(fetch_pages.parse_date("Oct 20, 2026"), dt.date(2026, 10, 20))
        self.assertIsNone(fetch_pages.parse_date("no date here"))
        self.assertIsNone(fetch_pages.parse_date("13/13/2026"))   # 非法月日不炸

    def test_fetch_pages_candidate_filter(self):
        # 两段式: 列表页带窗内日期 → 确认; 无日期但像文章 → 进验证队列(文章页验日期);
        # 出窗日期/异站/导航路径 → 直接丢弃
        import fetch_pages
        import datetime as dt
        today = dt.date(2026, 10, 25)
        links = [
            ("https://www.sag.gob.cl/noticias/nueva-resolucion-2026-10-24", "Resolución 24/10"),
            ("https://www.sag.gob.cl/noticias/old-2026-09-01", "antiguo 01/09"),
            ("https://www.sag.gob.cl/noticias/sin-fecha-titulo-largo-del-articulo", "sin fecha"),
            ("https://otro-sitio.cl/noticias/x-2026-10-24", "otro sitio"),
            ("https://www.sag.gob.cl/contacto", "contacto 2026-10-24"),
        ]
        entry = "https://www.sag.gob.cl/"
        confirmed, to_verify = fetch_pages.pick_candidates(links, entry, today)
        self.assertEqual([c[0] for c in confirmed],
                         ["https://www.sag.gob.cl/noticias/nueva-resolucion-2026-10-24"])
        # 无日期的文章型链接进验证队列(sin-fecha), 出窗的(09-01)与导航/异站不进
        self.assertEqual([c[0] for c in to_verify],
                         ["https://www.sag.gob.cl/noticias/sin-fecha-titulo-largo-del-articulo"])
        # verify_dates: 窗内文章页日期 → 保留; 窗外 → 丢弃(mock curl 免网络)
        def fake_curl(url, **kw):
            body = "<p>Publicado el 24/10/2026</p>" if "sin-fecha" in url else \
                   "<p>Publicado el 01/09/2026</p>"
            return 200, body
        with mock.patch.object(fetch_pages, "curl", side_effect=fake_curl):
            got = fetch_pages.verify_dates(to_verify, today)
        self.assertEqual([g[0] for g in got],
                         ["https://www.sag.gob.cl/noticias/sin-fecha-titulo-largo-del-articulo"])

    def test_extract_records_parse_and_validate(self):
        # key=value 输出 → 嵌套 source.*; quote >120 截断; 缺标题拒收
        import extract_records as er
        rec = er.parse_kv_output(
            "title_cn=泰国暂停进口\nsource_url=https://a.b/c\nsource_quote=" + "长" * 200 +
            "\ndim_trade=4\n这行没有等号\nimpact_type=约束")
        self.assertEqual(rec["title_cn"], "泰国暂停进口")
        self.assertEqual(rec["source"]["url"], "https://a.b/c")
        self.assertIsNone(er.validate(rec, "https://a.b/c"))
        self.assertEqual(len(rec["source"]["quote"]), 120)   # validate 截断而非拒收
        bad = {"source": {"url": "https://a.b/c", "quote": "x"}}
        self.assertEqual(er.validate(bad, "https://a.b/c"), "缺标题")
        wrong = {"title_cn": "x", "source": {"url": "https://evil/x", "quote": "x"}}
        self.assertIn("url 与存档不符", er.validate(wrong, "https://a.b/c"))

    def test_extract_records_429_fast_fail(self):
        # Token Plan 耗尽: 不烧第二次重试, 剩余文档留待下轮, search-log 留痕
        import extract_records as er
        raw_dir = os.path.join(TMP, "raw", "2026-10-26")
        os.makedirs(raw_dir, exist_ok=True)
        with open(os.path.join(raw_dir, "01-x.meta.json"), "w", encoding="utf-8") as f:
            json.dump({"title": "甲", "url": "https://official.example/a"}, f)
        with open(os.path.join(raw_dir, "01-x.txt"), "w", encoding="utf-8") as f:
            f.write("原文内容 甲政策 2026-10-20")
        calls = []

        def fake_call(prompt):
            calls.append(prompt)
            return 1, "HTTP 429: 当前已达到 Token Plan 用量上限"
        try:
            with mock.patch.object(er, "call_model", side_effect=fake_call), \
                    mock.patch.object(er, "DATA_DIR", TMP):
                n_ok, n_fail = er.extract("2026-10-26")
            self.assertEqual((n_ok, n_fail), (0, 1))
            self.assertEqual(len(calls), 1, "429 不得重试")
            with open(os.path.join(raw_dir, "search-log.md"), encoding="utf-8") as f:
                self.assertIn("Token Plan 耗尽", f.read())
        finally:
            shutil.rmtree(raw_dir, ignore_errors=True)

    def _run_scan_text(self):
        with open(os.path.join(REPO, "scripts", "run_scan.sh"), encoding="utf-8") as f:
            return f.read()

    def test_run_scan_p4_default_pipeline(self):
        # 默认确定性管线: fetch_pages(零LLM) + extract_records(微调用) + normalize/risk;
        # --agent 才走旧会话路径
        text = self._run_scan_text()
        self.assertIn("--agent", text)
        self.assertIn("AGENT_MODE", text)
        self.assertIn('"$REPO/scripts/fetch_pages.py" --date "$SCAN_DATE"', text)
        self.assertIn('"$REPO/scripts/extract_records.py" --date "$SCAN_DATE"', text)
        self.assertIn('--stage risk', text)
        # 默认(非 agent)分支必须先判定(elif AGENT_MODE != 1 在 Agent 分支体之前),
        # 否则默认路径永远走不到
        self.assertLess(text.index("fetch_pages.py"), text.index("死通道跳过名单"))
        self.assertLess(text.index("确定性加工(单轮微调用抽取)"),
                        text.index("加工出报(上限"))

    def test_extract_records_prompt_no_forbidden_chars(self):
        # prompt 经 shell 双引号拼进 hermes -z: 模板本身不得含 ASCII 双引号/反引号/$
        import extract_records as er
        prompt = er.build_prompt({"title": "t"}, "https://a.b/c", "正文" * 100, "2026-10-25")
        self.assertNotIn('"', prompt)
        self.assertNotIn("`", prompt)
        self.assertNotIn("$", prompt)


class BriefTextFocusTests(unittest.TestCase):
    """IM 卡片"重点关注"口径必须与 report.py 一致(2026-10-19 修复)。

    原实现只把"中影响 or 收紧"放进重点关注, 合同里最重的一档"高影响"被丢进
    "其他新增" —— 用户第一眼看的 IM 卡片反而把最该关注的政策藏起来了。
    """

    def setUp(self):
        import push_report
        self.pr = push_report

    def _card(self, level):
        data = {"new": [{"event_id": "a", "title_cn": "重点政策", "country_cn": "X",
                          "action_type": "调整", "impact_level": level,
                          "impact_type": "约束", "source": {"url": "https://x/1"}}],
                }
        return self.pr.brief_text("2026-10-19", data)

    def test_high_impact_goes_to_focus_section(self):
        txt = self._card("高影响")
        self.assertIn("重点关注", txt)
        focus_part = txt.split("重点关注", 1)[1].split("其他新增")[0]
        self.assertIn("重点政策", focus_part)

    def test_medium_impact_goes_to_focus_section(self):
        txt = self._card("中影响")
        focus_part = txt.split("重点关注", 1)[1].split("其他新增")[0]
        self.assertIn("重点政策", focus_part)

    def test_low_impact_stays_in_other(self):
        txt = self._card("低影响")
        self.assertNotIn("重点关注", txt)
        self.assertIn("重点政策", txt.split("其他新增", 1)[1])


class BriefTextContentTests(unittest.TestCase):
    """推送卡片内容口径(2026-10-27 用户定): 标题超链 + 正文是整个政策的简介。

    用户原话: "推送内容的检索标题可以超链, 内容是整个政策的简介" 以及
    "推微信侧的测试内容, 需要和正式推送钉钉的要一致, 不然就不是测试了"。
    两条合起来的约束:
      ① 每条 = 标题(指向原文的链接) + 简介全文(不截断);
      ② 所有渠道同一份文本 —— 微信侧审阅推送的就是钉钉那份正式内容。
    """

    def setUp(self):
        import push_report
        self.pr = push_report

    def _card(self, records, **kw):
        return self.pr.brief_text("2026-10-27", {"new": records}, **kw)

    def _rec(self, **kw):
        base = {"event_id": "e1", "title_cn": "测试政策", "country_cn": "美国",
                "action_type": "调整", "impact_type": "约束", "impact_level": "高影响",
                "summary_cn": "美国动植物卫生检验局通报得克萨斯州修订了新大陆螺旋蝇疫区范围。",
                "source": {"url": "https://example.gov/notice/1"}}
        base.update(kw)
        return base

    def test_title_is_hyperlink_to_source(self):
        txt = self._card([self._rec()])
        self.assertIn("[测试政策](https://example.gov/notice/1)", txt)

    def test_body_is_full_summary_not_truncated(self):
        summary = ("美国动植物卫生检验局通报得克萨斯州修订了新大陆螺旋蝇疫区的划定范围, "
                   "属进口活牛等易感动物检疫风险区管理的调整, 涉及州际运输与放行条件。")
        self.assertGreater(len(summary), 60, "样本简介应超过旧口径的 60 字截断线")
        txt = self._card([self._rec(summary_cn=summary)])
        self.assertIn(summary, txt)
        self.assertIn("简介:", txt)

    def test_rationale_no_longer_replaces_summary(self):
        # 研判理由只留在完整报告里, 不再截断后顶替卡片正文
        rationale = "研判理由" * 20
        txt = self._card([self._rec(impact_rationale=rationale)])
        self.assertNotIn(rationale, txt)

    def test_entries_separated_by_blank_line(self):
        # 微信单条 2000 字且按 markdown 块打包: 条目之间不留空行, 整段超限会被
        # 静默截断(丢政策); 留空行后每个条目自成一个块, 按条目边界切成多条。
        txt = self._card([self._rec(event_id="e1"),
                          self._rec(event_id="e2", title_cn="第二条政策")])
        self.assertIn("\n\n- [第二条政策]", txt)

    def test_missing_url_has_no_empty_link(self):
        txt = self._card([self._rec(source={})])
        self.assertNotIn("]()", txt)
        self.assertIn("测试政策", txt)

    def test_summary_falls_back_to_english_then_empty(self):
        txt = self._card([self._rec(summary_cn=None, summary_en="English summary line")])
        self.assertIn("English summary line", txt)
        txt2 = self._card([self._rec(summary_cn=None)])
        self.assertNotIn("简介:", txt2)

    def test_all_channels_receive_identical_body(self):
        # "测试=正式": 微信侧审阅推送与钉钉正式推送必须是同一份内容
        sent = []

        def rec(name, idx):
            def fn(*a, **kw):
                sent.append((name, a[idx]))
            return fn

        env = {"WECHAT_WEBHOOK": "https://qyapi.example/x",
               "DINGTALK_WEBHOOK": "https://oapi.example/x",
               "SMTP_HOST": "smtp.example.com"}
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(self.pr, "send_wechat", side_effect=rec("企业微信", 0)), \
             mock.patch.object(self.pr, "send_dingtalk", side_effect=rec("钉钉", 0)), \
             mock.patch.object(self.pr, "send_mail", side_effect=rec("邮箱", 1)), \
             mock.patch.object(self.pr, "send_hermes", side_effect=rec("微信", 0)), \
             mock.patch.object(self.pr, "channel_available", return_value=True):
            channels = self.pr._build_channels("统一正文", "2026-10-27", None,
                                               extra_keys=["weixin"])
            for _name, fn in channels:
                fn()
        self.assertTrue(sent, "没有任何渠道被调用")
        for name, body in sent:
            self.assertEqual(body, "统一正文", name)

    def test_cut_marks_truncation_instead_of_silent_drop(self):
        # 渠道字节上限截断必须显式标注(与"Word 附件缺失如实标注"同一口径)
        out = self.pr._cut("A" * 10000, 3800)
        self.assertIn("已截断", out)
        self.assertLessEqual(len(out.encode("utf-8")), 3800)
        self.assertEqual(self.pr._cut("短文本", 3800), "短文本")


class NonPolicyScreenTests(unittest.TestCase):
    """非政策内容闸门(2026-10-27)—— 会议/表彰/装运/栏目页不得当成政策入库。

    背景: 10-25~27 连续三天, 同一批非政策内容被当成政策入库(表彰福布斯 3 份、
    CPM 局会议 3 份、首批输华牛肉启运 3 份)。根因是 P4 管线的 prompt 只有字段清单,
    「只收变化」的规则没进上下文(与 AGENTS.md 记的 92dbe1e 缺口同源)。
    """

    def setUp(self):
        import extract_records
        self.ex = extract_records

    def _rec(self, **kw):
        base = {"title_cn": "", "summary_cn": "", "source": {"quote": ""}}
        base.update(kw)
        return base

    def test_model_says_no_is_rejected(self):
        reject, why = self.ex.screen_non_policy(self._rec(
            is_policy_change="no", non_policy_note="表彰新闻",
            title_cn="巴西农业部长表彰入选福布斯全球农业百强榜的女性同仁"))
        self.assertTrue(reject)
        self.assertIn("非政策", why)

    def test_meeting_notice_rejected(self):
        reject, _ = self.ex.screen_non_policy(self._rec(
            title_cn="国际植物保护公约战略规划小组（SPG）会议前瞻",
            summary_cn="国际植物保护公约发布战略规划小组会议预告，属多边植物检疫治理议程安排。"))
        self.assertTrue(reject)

    def test_cpm_bureau_meeting_rejected(self):
        reject, _ = self.ex.screen_non_policy(self._rec(
            title_cn="国际植保公约植物检疫措施委员会（CPM）局将在巴西举行线下会议",
            summary_cn="国际植保公约通报其植物检疫措施委员会局将在巴西举行线下会议，属机构运行安排通报。"))
        self.assertTrue(reject)

    def test_column_page_rejected(self):
        reject, _ = self.ex.screen_non_policy(self._rec(
            title_cn="土耳其农业产区（Tarım Havzaları）生产与支持模式",
            summary_cn="土耳其农业与林业部栏目页面，摘录仅含页面关键词元数据。"))
        self.assertTrue(reject)

    def test_leader_attending_shipment_rejected(self):
        reject, _ = self.ex.screen_non_policy(self._rec(
            title_cn="巴西农业部长安德烈·德·保拉陪同东北部首批输华牛肉启运",
            summary_cn="巴西农业部长赴桑托斯港见证东北部首批输华牛肉装船发运。"))
        self.assertTrue(reject)

    def test_self_negating_summary_rejected(self):
        reject, _ = self.ex.screen_non_policy(self._rec(
            title_cn="国际植物保护公约活动公告页(2026-10-27)",
            summary_cn="存档为IPPC活动页面，摘录内容仅为DataTable脚本，未见具体检疫政策措施文本。"))
        self.assertTrue(reject)

    def test_real_policy_kept(self):
        reject, why = self.ex.screen_non_policy(self._rec(
            title_cn="得克萨斯州修订新大陆螺旋蝇疫区范围",
            summary_cn="美国动植物卫生检验局通报得克萨斯州修订了新大陆螺旋蝇疫区的划定范围。"))
        self.assertFalse(reject, why)

    def test_meeting_that_adopts_standard_kept(self):
        # 会议本身不是措施, 但"会议通过新标准"是措施 —— 不能误杀
        reject, why = self.ex.screen_non_policy(self._rec(
            title_cn="植物检疫措施委员会第71次会议通过新标准",
            summary_cn="国际植物保护公约植物检疫措施委员会通过两项新的植物检疫标准。"))
        self.assertFalse(reject, why)

    def test_sanitary_measure_kept(self):
        reject, why = self.ex.screen_non_policy(self._rec(
            title_cn="因巴西禽场检测到新城疫实施的卫生措施",
            summary_cn="阿根廷因巴西禽场检测到新城疫疫情宣布对巴西禽类及相关产品实施卫生限制措施。",
            source={"quote": "Medidas sanitarias por deteccion de enfermedad de Newcastle"}))
        self.assertFalse(reject, why)

    def test_missing_flag_falls_back_to_keywords(self):
        # 模型漏填 is_policy_change 时, 关键词兜底仍要拦下表彰新闻
        reject, _ = self.ex.screen_non_policy(self._rec(
            title_cn="农业部长向入选福布斯百强农业女性致敬",
            summary_cn="巴西农业部网站发布新闻，宣布部长表彰入选福布斯全球农业百强影响力人物。"))
        self.assertTrue(reject)

    def test_prompt_carries_scope_rules(self):
        # 规则必须真的进 prompt —— 否则模型无从判断(92dbe1e 同源缺口)
        prompt = self.ex.build_prompt({"title": "t"}, "https://x/1", "正文", "2026-10-27")
        self.assertIn("is_policy_change", prompt)
        self.assertIn("表彰", prompt)
        self.assertIn("只监测", prompt)

    def test_rejected_urls_count_as_done(self):
        # 拒收的 URL 也要算"已处理": 否则明天重抽同一篇, 白烧一次 LLM 调用
        raw = tempfile.mkdtemp(prefix="rejected-")
        day = os.path.join(raw, "raw", "2026-10-27")
        os.makedirs(day)
        with open(os.path.join(day, "rejected-policy.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"url": "https://x/1", "reason": "非政策内容"},
                               ensure_ascii=False) + "\n")
        with mock.patch.object(self.ex, "DATA_DIR", raw):
            urls = self.ex.done_urls("2026-10-27")
        self.assertIn("https://x/1", urls)


class FalsePositiveExclusionTests(unittest.TestCase):
    """false_positive(非政策内容/误报)不进"今日新增", 但在待核实栏留痕。"""

    def setUp(self):
        import normalize
        import report
        self.n, self.report = normalize, report
        self.db = os.path.join(tempfile.mkdtemp(prefix="fp-"), "t.db")

    def _insert(self, eid, status, title):
        con = self.n.open_db(self.db)
        e = self.n.fill_defaults(_source_event(event_id=eid, title_cn=title))
        e["verification_status"] = status
        e["first_seen"] = "2026-10-27T08:00:00"
        con.execute("INSERT OR REPLACE INTO events "
                    "(event_id,payload,category,event_date,record_type,verification_status) "
                    "VALUES (?,?,?,?,?,?)",
                    (eid, json.dumps(e, ensure_ascii=False), "policy",
                     e["event_date"], "policy", status))
        con.commit()
        con.close()

    def test_false_positive_excluded_from_new_but_kept_in_flagged(self):
        self._insert("e-real", "unverified", "真政策")
        self._insert("e-fp", "false_positive", "表彰新闻")
        data = self.report.collect("2026-10-27", self.db)
        titles = [e.get("title_cn") for e in data["new"]]
        self.assertIn("真政策", titles)
        self.assertNotIn("表彰新闻", titles)
        flagged = [e.get("title_cn") for e in data.get("flagged") or []]
        self.assertIn("表彰新闻", flagged)

    def test_open_items_shows_false_positive_with_chinese_label(self):
        e = {"verification_status": "false_positive", "title_cn": "表彰新闻",
             "country_cn": "巴西", "event_id": "e-fp", "summary_cn": "纯表彰新闻"}
        txt = self.report.make_open_items([e])
        self.assertIn("非政策内容", txt)
        self.assertNotIn("false_positive", txt)

    def test_push_card_excludes_false_positive(self):
        # 推送卡片只放真政策(卡片正文 = 今日新增)
        import push_report
        data = {"new": [{"event_id": "e-real", "title_cn": "真政策", "country_cn": "X",
                         "action_type": "调整", "impact_level": "低影响",
                         "impact_type": "中性", "summary_cn": "真政策摘要",
                         "source": {"url": "https://x/1"}}],
                "flagged": [{"event_id": "e-fp", "title_cn": "表彰新闻"}]}
        txt = push_report.brief_text("2026-10-27", data)
        self.assertIn("真政策", txt)
        self.assertNotIn("表彰新闻", txt)


class NormalizeValidateTests(unittest.TestCase):
    """P1#2: event_id 关联对象必填, 缺时 validate 拒收。"""

    def setUp(self):
        from normalize import validate
        self.validate = validate

    def test_valid_minimal_record_passes(self):
        self.assertEqual(self.validate(_source_event()), [])

    def test_rejects_when_all_subject_fields_empty(self):
        e = _source_event(products=[], target_countries=[],
                          disease_name_cn=None, disease_name_en=None,
                          scope=None, policy_key=None)
        errs = self.validate(e)
        self.assertTrue(any("关联对象必填" in m for m in errs), errs)

    def test_policy_key_alone_satisfies_subject(self):
        e = _source_event(products=[], target_countries=[],
                          disease_name_cn=None, disease_name_en=None,
                          scope=None, policy_key="internal-training|2026-Q3")
        self.assertEqual(self.validate(e), [])

    def test_each_subject_field_alone_satisfies(self):
        # products / target_countries / disease_name_en / scope 任何一个非空即可
        for kw in (
            {"products": ["种苗"]},
            {"target_countries": ["日本"]},
            {"disease_name_en": "foot-and-mouth disease"},
            {"scope": "全境口岸"},
        ):
            overrides = {"products": [], "target_countries": [],
                         "disease_name_cn": None, "disease_name_en": None,
                         "scope": None, "policy_key": None}
            overrides.update(kw)
            errs = self.validate(_source_event(**overrides))
            self.assertEqual(errs, [], (kw, errs))


class NormalizeEventIdTests(unittest.TestCase):
    """P1#2: 关联对象能区分同日同类政策, event_id 才稳定。"""

    def setUp(self):
        from normalize import event_id_of
        self.event_id_of = event_id_of

    def test_same_input_yields_same_id(self):
        e = _source_event()
        self.assertEqual(self.event_id_of(e), self.event_id_of(dict(e)))

    def test_different_products_diverge(self):
        a = self.event_id_of(_source_event(products=["新鲜牛肉"]))
        b = self.event_id_of(_source_event(products=["禽肉"]))
        self.assertNotEqual(a, b)

    def test_different_country_diverge(self):
        a = self.event_id_of(_source_event(country_cn="A国", country_en="A Land"))
        b = self.event_id_of(_source_event(country_cn="B国", country_en="B Land"))
        self.assertNotEqual(a, b)

    def test_different_date_diverge(self):
        a = self.event_id_of(_source_event(event_date="2026-09-15", effective_date="2026-09-15"))
        b = self.event_id_of(_source_event(event_date="2026-09-16", effective_date="2026-09-16"))
        self.assertNotEqual(a, b)

    def test_policy_key_used_as_subject(self):
        a = self.event_id_of(_source_event(policy_key="subject-a"))
        b = self.event_id_of(_source_event(policy_key="subject-b"))
        self.assertNotEqual(a, b)


class NormalizeMergeTests(unittest.TestCase):
    """合并更新保留 event_id 与 first_seen。"""

    def test_merge_preserves_identity_and_carries_new_field(self):
        from normalize import open_db, upsert, fill_defaults
        con = open_db()
        try:
            a = upsert(con, fill_defaults(dict(_source_event())), merge=True)
            first_id, first_seen = a["event_id"], a["first_seen"]
            b = upsert(con, fill_defaults(dict(_source_event(summary_cn="更新后的摘要"))), merge=True)
            self.assertEqual(b["event_id"], first_id)
            self.assertEqual(b["first_seen"], first_seen)
            self.assertEqual(b["summary_cn"], "更新后的摘要")
        finally:
            con.close()

    def test_fill_default_sentinels_do_not_overwrite_real_conclusions(self):
        # 2026-10-24 体检 H4: DEFAULT_SENTINELS 原先缺 action_type/policy_domain,
        # 同一显式 event_id(镜像回放/增量更新路径)二轮重抽没识别出动作/领域时,
        # fill_defaults 的兜底 "调整"/"measures" 会把库里的 "收紧"/"animal" 洗掉
        # —— 违反"空值和兜底默认值不覆盖已有核验/研判结论"的合同。
        from normalize import open_db, upsert, fill_defaults
        con = open_db()
        ev_id = "5entinel0test"
        try:
            first = fill_defaults(dict(_source_event(event_id=ev_id)))
            self.assertEqual(first["action_type"], "收紧")
            upsert(con, first, merge=True)
            # 第二轮: 同一 event_id, 但 action_type/policy_domain 缺失,
            # fill_defaults 会各填兜底值 "调整"/"measures"
            second = _source_event(event_id=ev_id)
            second.pop("action_type")
            second.pop("policy_domain")
            second = fill_defaults(dict(second))
            self.assertEqual(second["action_type"], "调整")  # 兜底确实已填
            merged = upsert(con, second, merge=True)
            self.assertEqual(merged["action_type"], "收紧")   # 库里真实结论保住了
            self.assertEqual(merged["policy_domain"], "animal")
        finally:
            con.execute("DELETE FROM events WHERE event_id = ?", (ev_id,))
            con.commit()
            con.close()


class _FixedClock:
    """替 normalize.datetime: 固定 today/now, 让时间窗测试与真实时钟解耦。

    utcnow 固定为 2026-10-11 16:00 UTC —— 换算到东八区(UTC+8)正好是
    2026-10-12 00:00, 即业务日期 2026-10-12。这样 biz_today()/biz_now()
    走 utcnow + 8h 的路径也能被固定住, 不会意外用到真实时钟。
    """

    timedelta = datetime.timedelta

    class date:
        @staticmethod
        def today():
            return datetime.date(2026, 10, 12)

        @staticmethod
        def fromisoformat(value):
            return datetime.date.fromisoformat(value)

    class datetime:
        @staticmethod
        def now():
            return datetime.datetime(2026, 10, 12, 8, 0, 0)

        @staticmethod
        def utcnow():
            return datetime.datetime(2026, 10, 11, 16, 0, 0)


class BizClockTests(unittest.TestCase):
    """业务时区(东八区): 生产 VPS 系统时区是 Etc/UTC, 直接用 date.today()
    会让北京时间早间跑批整体错一天(2026-10 实测 10-15 扫描产出 2026-10-14 报告)。"""

    def setUp(self):
        import normalize
        self.normalize = normalize

    def test_biz_today_is_utc_plus_8(self):
        # 用真实时钟验证换算方向: 东八区日期应 >= UTC 日期, 差值不超过 1 天
        utc_day = datetime.datetime.utcnow().date()
        biz = self.normalize.biz_today()
        delta = (datetime.date.fromisoformat(biz) - utc_day).days
        self.assertIn(delta, (0, 1), (utc_day, biz))

    def test_biz_now_offset_is_8_hours(self):
        delta = datetime.datetime.fromisoformat(self.normalize.biz_now()) \
            - datetime.datetime.utcnow()
        self.assertGreater(delta.total_seconds(), 7.5 * 3600)
        self.assertLess(delta.total_seconds(), 8.5 * 3600)

    def test_biz_today_returns_iso_string(self):
        self.assertRegex(self.normalize.biz_today(), r"^\d{4}-\d{2}-\d{2}$")

    def test_now_uses_biz_timezone(self):
        # now() 应与 biz_now 同源, 不能退回宿主机本地时区
        self.assertEqual(self.normalize.now(),
                         (datetime.datetime.utcnow()
                          + datetime.timedelta(hours=8)).isoformat(timespec="seconds")[:19]
                         + self.normalize.now()[19:])


class NormalizeStaleGateTests(unittest.TestCase):
    """旧闻拦截: 发布时间(source.publish_date, 缺省 event_date)超出时间窗的新记录拒收。"""

    TODAY = datetime.date(2026, 10, 12)  # 与 _FixedClock 一致

    def setUp(self):
        import normalize
        self.normalize = normalize

    def test_published_date_prefers_source_over_event_date(self):
        e = _source_event(source={"tier": 1, "name": "s", "url": "https://example.com/p9",
                                  "publish_date": "2026-10-10", "quote": "q"},
                          event_date="2026-01-28", effective_date="2026-01-28")
        self.assertEqual(self.normalize.published_date(e), "2026-10-10")

    def test_published_date_falls_back_to_event_date(self):
        e = _source_event(source={"tier": 1, "name": "s", "url": "https://example.com/p9",
                                  "publish_date": None, "quote": "q"})
        self.assertEqual(self.normalize.published_date(e), "2026-09-15")

    def test_stale_reason_rejects_old_publish_date(self):
        # 复现测试漏洞: 1 月旧公告在 10 月被检索到, 页面常挂网但发布时间早于窗口
        e = _source_event(event_date="2026-01-28", effective_date="2026-01-28",
                          source={"tier": 1, "name": "APHIS", "url": "https://example.com/old",
                                  "publish_date": "2026-01-28", "quote": "q"})
        reason = self.normalize.stale_reason(e, "2026-10-05")
        self.assertIsNotNone(reason)
        self.assertIn("早于窗口下限", reason)

    def test_stale_reason_passes_recent_record(self):
        e = _source_event(source={"tier": 1, "name": "s", "url": "https://example.com/p8",
                                  "publish_date": "2026-10-10", "quote": "q"})
        self.assertIsNone(self.normalize.stale_reason(e, "2026-10-05"))

    def test_stale_reason_fresh_page_with_old_event_date_passes(self):
        # 今日新发布的公告可以引用更早的生效日, 不能按 event_date 反推旧闻
        e = _source_event(event_date="2026-09-01", effective_date="2026-09-01",
                          source={"tier": 1, "name": "s", "url": "https://example.com/p7",
                                  "publish_date": "2026-10-12", "quote": "q"})
        self.assertIsNone(self.normalize.stale_reason(e, "2026-10-05"))

    def test_stale_reason_pads_month_granularity(self):
        e = _source_event(event_date="2026-01", effective_date="2026-01",
                          source={"tier": 1, "name": "s", "url": "https://example.com/p6",
                                  "publish_date": "2026-01", "quote": "q"})
        self.assertIsNotNone(self.normalize.stale_reason(e, "2026-10-05"))

    def test_stale_reason_disabled_without_cutoff(self):
        e = _source_event(event_date="2020-01-01", effective_date="2020-01-01")
        self.assertIsNone(self.normalize.stale_reason(e, None))

    def _run_main(self, argv):
        with mock.patch("sys.argv", ["normalize.py"] + argv), \
                mock.patch("normalize.datetime", _FixedClock), \
                redirect_stdout(io.StringIO()):
            return self.normalize.main()

    def test_main_gate_rejects_new_stale_but_merges_known_id(self):
        tmp = tempfile.mkdtemp(prefix="radar-gate-")
        db = os.path.join(tmp, "gate.db")
        inp = os.path.join(tmp, "in.json")
        out = os.path.join(tmp, "out")

        def write_input(events):
            with open(inp, "w", encoding="utf-8") as f:
                json.dump(events, f, ensure_ascii=False)

        # 1) 关窗(--max-age-days 0)先入库一条 9 月记录, 模拟历史已入库
        write_input([_source_event()])
        self.assertEqual(self._run_main(
            ["--input", inp, "--out", out, "--db-path", db, "--max-age-days", "0"]), 0)

        # 2) 已入库 event_id 的合并更新(核验/研判回写)不受默认 7 天窗口影响
        write_input([_source_event(summary_cn="核验后的更新")])
        self.assertEqual(self._run_main(
            ["--input", inp, "--out", out, "--db-path", db]), 0)
        con = self.normalize.open_db(db)
        try:
            rows = self.normalize.load_events(con)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["summary_cn"], "核验后的更新")
        finally:
            con.close()

        # 3) 新 event_id + 旧发布时间(APHIS 旧闻场景) → 默认窗口拒收, 不入库
        write_input([_source_event(
            products=["马科动物"], event_date="2026-01-28", effective_date="2026-01-28",
            source={"tier": 1, "name": "APHIS", "url": "https://www.aphis.usda.gov/old-news",
                    "publish_date": "2026-01-28", "quote": "postpones implementation"})])
        self.assertEqual(self._run_main(
            ["--input", inp, "--out", out, "--db-path", db]), 1)
        con = self.normalize.open_db(db)
        try:
            self.assertEqual(len(self.normalize.load_events(con)), 1)
        finally:
            con.close()

        # 4) 显式放宽窗口后可回补(旧闻逃生口)
        self.assertEqual(self._run_main(
            ["--input", inp, "--out", out, "--db-path", db, "--max-age-days", "365"]), 0)
        con = self.normalize.open_db(db)
        try:
            self.assertEqual(len(self.normalize.load_events(con)), 2)
        finally:
            con.close()


class RiskContractTests(unittest.TestCase):
    """scripts/risk.py 加权合同: score/level/focus 三者一致, 0.05 容差。"""

    def setUp(self):
        from risk import derive_missing, validate_result, weighted_score, level_of
        self.derive_missing = derive_missing
        self.validate_result = validate_result
        self.weighted_score = weighted_score
        self.level_of = level_of

    def test_weighted_score_matches_prompt_formula(self):
        # trade*0.35 + biosecurity*0.30 + response*0.25 + alignment*0.10
        dims = {"trade": 4, "biosecurity": 3, "response": 2, "alignment": 1}
        self.assertAlmostEqual(self.weighted_score(dims),
                               4 * 0.35 + 3 * 0.30 + 2 * 0.25 + 1 * 0.10, places=2)

    def test_level_thresholds(self):
        self.assertEqual(self.level_of(3.5), "高影响")
        self.assertEqual(self.level_of(3.49), "中影响")
        self.assertEqual(self.level_of(2.0), "中影响")
        self.assertEqual(self.level_of(1.99), "低影响")

    def test_derive_missing_fills_chain_from_full_dims(self):
        r = {"dimension_scores": {"trade": 4, "biosecurity": 4, "response": 4, "alignment": 4}}
        r = self.derive_missing(r)
        self.assertEqual(r["impact_score"], round(4 * 0.35 + 4 * 0.30 + 4 * 0.25 + 4 * 0.10, 2))
        self.assertEqual(r["impact_level"], "高影响")
        self.assertEqual(r["impact_focus"], "立即关注")

    def test_derive_partial_dims_does_not_invent_score(self):
        r = {"dimension_scores": {"trade": 4, "biosecurity": 3, "response": 3}}
        r = self.derive_missing(r)
        self.assertIsNone(r.get("impact_score"))
        self.assertIsNone(r.get("impact_level"))

    def test_validate_rejects_score_level_mismatch(self):
        r = {"dimension_scores": {"trade": 4, "biosecurity": 3, "response": 3, "alignment": 2},
             "impact_type": "约束", "impact_level": "高影响",
             "china_relevance": "间接影响", "recommended_action": "x",
             "impact_rationale": "y", "impact_score": 3.3, "impact_focus": "立即关注"}
        errs = self.validate_result(r)
        self.assertTrue(
            any("impact_level" in m and "impact_score" in m for m in errs), errs)

    def test_validate_rejects_focus_mismatch(self):
        r = {"dimension_scores": {"trade": 1, "biosecurity": 1, "response": 1, "alignment": 1},
             "impact_type": "中性", "impact_level": "低影响",
             "china_relevance": "暂无明显关联", "recommended_action": "x",
             "impact_rationale": "y", "impact_score": 1.0, "impact_focus": "持续观察"}
        errs = self.validate_result(r)
        self.assertTrue(any("impact_focus" in m for m in errs), errs)

    def test_validate_rejects_dim_out_of_range(self):
        r = {"dimension_scores": {"trade": 6, "biosecurity": 3, "response": 3, "alignment": 2},
             "impact_type": "约束", "impact_level": "高影响",
             "china_relevance": "间接影响", "recommended_action": "x",
             "impact_rationale": "y", "impact_score": 4.0, "impact_focus": "立即关注"}
        errs = self.validate_result(r)
        self.assertTrue(any("0-5" in m for m in errs), errs)

    def test_validate_rejects_extra_dimension_keys(self):
        r = {"dimension_scores": {"trade": 3, "biosecurity": 3, "response": 3,
                                  "alignment": 3, "extra": 3},
             "impact_type": "中性", "impact_level": "中影响",
             "china_relevance": "间接影响", "recommended_action": "x",
             "impact_rationale": "y", "impact_score": 3.0, "impact_focus": "持续观察"}
        errs = self.validate_result(r)
        self.assertTrue(any("dimension_scores" in m and "只允许" in m for m in errs), errs)

    def test_valid_result_passes(self):
        r = {"dimension_scores": {"trade": 4, "biosecurity": 3, "response": 3, "alignment": 2},
             "impact_type": "约束", "impact_level": "中影响",
             "china_relevance": "间接影响", "recommended_action": "核查准入",
             "impact_rationale": "依据xxx", "impact_score": 3.25, "impact_focus": "持续观察"}
        self.assertEqual(self.validate_result(r), [])


class ReportDateGuardTests(unittest.TestCase):
    """非法 --date 不得让 report/push 崩裸 TypeError(2026-10-19 修复)。

    复现: A3 修复把 _date() 改成"解析失败返回 None", 但 collect() 拿它做日期减法
    (`_date(date) - timedelta`), 库里非空时 `report.py --date 2026-13-45` 与
    `push_report.py --date 2026-13-45` 都抛 TypeError, 当天日报与推送一起丢。
    """

    def setUp(self):
        import normalize
        import report
        self.n = normalize
        self.report = report
        # 造一条非空库(空库时 collect 提前 return None, 走不到日期运算)
        self.con = self.n.open_db()
        e = self.n.fill_defaults(_source_event())
        self.con.execute(
            "INSERT OR REPLACE INTO events (event_id,payload,category,event_date,record_type) "
            "VALUES (?,?,?,?,?)",
            (e["event_id"], json.dumps(e, ensure_ascii=False), "policy",
             e["event_date"], "policy"))
        self.con.commit()
        self.con.close()

    def test_collect_raises_value_error_on_bad_date(self):
        with self.assertRaises(ValueError):
            self.report.collect("2026-13-45")

    def test_collect_accepts_valid_date(self):
        data = self.report.collect("2026-10-19")
        self.assertIsNotNone(data)
        self.assertIn("new", data)

    def test_report_main_prints_chinese_error_and_exits_2(self):
        with mock.patch("sys.argv", ["report.py", "--date", "2026-13-45"]), \
                redirect_stdout(io.StringIO()) as buf:
            rc = self.report.main()
        self.assertEqual(rc, 2)
        self.assertIn("日期格式非法", buf.getvalue())


class RiskNullDimsTests(unittest.TestCase):
    """dimension_scores 为 null 时不得崩(2026-10-19 修复)。

    复现: derive_missing 用 setdefault 归一, 而 setdefault 不覆盖显式 null;
    Agent 研判输出 "dimension_scores": null 时 dims_complete(None) 抛 TypeError,
    risk.py --from-file 整批中断。
    """

    def setUp(self):
        import risk
        self.risk = risk

    def test_derive_missing_handles_null_dims(self):
        out = self.risk.derive_missing({"dimension_scores": None})
        self.assertEqual(out["dimension_scores"], {})
        self.assertIsNone(out.get("impact_score"))

    def test_derive_missing_handles_non_dict_dims(self):
        out = self.risk.derive_missing({"dimension_scores": "trade=4"})
        self.assertEqual(out["dimension_scores"], {})

    def test_fill_defaults_normalizes_null_dims(self):
        import normalize
        e = normalize.fill_defaults(_source_event(dimension_scores=None))
        self.assertEqual(e["dimension_scores"], {})

    def test_from_file_batch_survives_one_bad_record(self):
        # 一条坏记录不得让整批写回中断(与 report.py 的 A3 同理)
        import normalize
        import risk
        con = normalize.open_db()
        try:
            good = normalize.fill_defaults(_source_event(products=["种苗"]))
            con.execute(
                "INSERT OR REPLACE INTO events (event_id,payload,category,event_date,record_type) "
                "VALUES (?,?,?,?,?)",
                (good["event_id"], json.dumps(good, ensure_ascii=False), "policy",
                 good["event_date"], "policy"))
            con.commit()
            payload = [
                {"event_id": "no-such-event-id", "impact_type": "约束",
                 "impact_level": "中影响", "china_relevance": "间接影响",
                 "recommended_action": "x", "impact_rationale": "y",
                 "impact_score": 3.0, "impact_focus": "持续观察",
                 "dimension_scores": None},
                {"event_id": good["event_id"], "impact_type": "约束",
                 "impact_level": "中影响", "china_relevance": "间接影响",
                 "recommended_action": "核查准入", "impact_rationale": "依据",
                 "impact_score": 3.25, "impact_focus": "持续观察",
                 "dimension_scores": {"trade": 4, "biosecurity": 3,
                                      "response": 3, "alignment": 2}},
            ]
            path = os.path.join(TMP, "impact-batch.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            with mock.patch("sys.argv", ["risk.py", "--from-file", path]), \
                    redirect_stdout(io.StringIO()):
                rc = risk.main()
            # 第一条不存在 -> 失败; 第二条必须已经写回
            rows = normalize.load_events(con, "event_id = ?", (good["event_id"],))
            self.assertEqual(rc, 1)
            self.assertTrue(rows)
            self.assertEqual(rows[0].get("impact_level"), "中影响")
        finally:
            con.close()


class NormalizeInputGuardTests(unittest.TestCase):
    """入库输入结构校验: events 必须是数组(2026-10-19 新增)。"""

    def test_events_must_be_list(self):
        import normalize
        path = os.path.join(TMP, "bad-shape.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"events": {"a": 1}}, f)
        with self.assertRaises(ValueError):
            normalize._load_input(path)

    def test_list_input_ok(self):
        import normalize
        path = os.path.join(TMP, "ok-shape.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump([_source_event()], f, ensure_ascii=False)
        self.assertEqual(len(normalize._load_input(path)), 1)


class DirtyDateGuardTests(unittest.TestCase):
    """A3 闭环(2026-10-19): 脏日期不许进库, 也不许绕过旧闻窗口, 更不许原样端进日报。

    复现: 旧实现只按**长度**判日期, "2026-13-45"(10 位, 但 13 月 45 日)会被
    原样返回; 旧闻窗口用字符串比较, "2026-13-45" > "2026-10-12" → 判为"比窗口
    还新"→ **脏日期反而绕过了拦截入库**, 然后日报表格里原样出现 2026-13-45。
    """

    def setUp(self):
        import normalize
        self.n = normalize

    def test_norm_date_rejects_semantic_invalid(self):
        for bad in ("2026-13-45", "2026-02-30", "2026-00-01", "TBD", "2026-1-5", "未知"):
            self.assertIsNone(self.n._norm_date(bad), bad)

    def test_norm_date_accepts_legal_granularity(self):
        self.assertEqual(self.n._norm_date("2026"), "2026-01-01")
        self.assertEqual(self.n._norm_date("2026-10"), "2026-10-01")
        self.assertEqual(self.n._norm_date("2026-10-19"), "2026-10-19")
        # ISO 日期时间取日期部分
        self.assertEqual(self.n._norm_date("2026-10-19T08:30:00"), "2026-10-19")

    def test_validate_rejects_dirty_event_date(self):
        e = _source_event(event_date="2026-13-45", effective_date="2026-13-45")
        errs = self.n.validate(e)
        self.assertTrue(any("不是合法日期" in m for m in errs), errs)

    def test_validate_rejects_tbd_date(self):
        e = _source_event(event_date="TBD", effective_date="TBD")
        errs = self.n.validate(e)
        self.assertTrue(any("不是合法日期" in m for m in errs), errs)

    def test_validate_accepts_month_and_year_granularity(self):
        e = _source_event(event_date="2026-10", effective_date="2026-10")
        self.assertEqual(self.n.validate(e), [])
        e2 = _source_event(event_date="2026", effective_date="2026")
        self.assertEqual(self.n.validate(e2), [])

    def test_dirty_date_cannot_bypass_stale_gate(self):
        # 关键回归: 脏日期曾因字符串比较而被判为"比窗口还新"从而放行
        e = _source_event(event_date="2026-13-45", effective_date="2026-13-45",
                          source={"tier": 1, "name": "s", "url": "https://www.aphis.usda.gov/x",
                                  "publish_date": "2026-13-45", "quote": "q"})
        reason = self.n.stale_reason(e, "2026-10-05")
        self.assertIsNotNone(reason)
        self.assertIn("日期不明", reason)

    def test_bad_publish_date_falls_back_to_event_date(self):
        e = _source_event(event_date="2026-09-15", effective_date="2026-09-15",
                          source={"tier": 1, "name": "s", "url": "https://www.aphis.usda.gov/y",
                                  "publish_date": "2026-13-45", "quote": "q"})
        self.assertEqual(self.n.published_date(e), "2026-09-15")

    def test_report_policy_date_marks_dirty(self):
        import report
        self.assertEqual(report.policy_date({"event_date": "2026-13-45"}), "日期异常")
        self.assertEqual(report.policy_date({"event_date": "TBD"}), "日期异常")
        self.assertEqual(report.policy_date({"effective_date": "2026-13-45",
                                             "event_date": "2026-10-19"}), "2026-10-19")
        self.assertEqual(report.policy_date({"event_date": "2026-10-19"}), "2026-10-19")
        self.assertEqual(report.policy_date({"effective_date": "2026-10",
                                             "event_date": "2026-10-19"}), "2026-10-19")

    def test_report_table_shows_date_anomaly(self):
        import report
        row = report.policy_row(1, {"country_cn": "X", "action_type": "收紧",
                                     "policy_domain": "animal", "event_date": "2026-13-45",
                                     "title_cn": "脏日期政策"})
        self.assertIn("日期异常", row)
        self.assertNotIn("2026-13-45", row)

    def test_dirty_date_record_still_in_todays_new_list_with_warning(self):
        # 脏日期不能让今天的政策从日报里消失, 但必须打 WARN
        import report
        con = self.n.open_db()
        e = self.n.fill_defaults(_source_event(event_date="2026-10-19",
                                                effective_date="2026-10-19"))
        e["event_date"] = "2026-13-45"      # 模拟存量脏数据(入库端现已拒收)
        e["effective_date"] = "2026-13-45"
        e["first_seen"] = "2026-10-19T08:00:00"
        con.execute("INSERT OR REPLACE INTO events (event_id,payload,category,event_date,record_type) "
                    "VALUES (?,?,?,?,?)",
                    (e["event_id"], json.dumps(e, ensure_ascii=False), "policy",
                     "2026-10-19", "policy"))
        con.commit()
        con.close()
        buf = io.StringIO()
        with redirect_stdout(buf):
            data = report.collect("2026-10-19")
        self.assertEqual(len(data["new"]), 1)
        self.assertIn("日期无法解析", buf.getvalue())


class ParseEpingTests(unittest.TestCase):
    """scripts/parse_eping.py: ePing 通报预筛器(2026-10-22 新增)。

    背景: 10-22 的 Agent 拉回 ePing 全量 JSON 后**没有用 dist/kw 预筛**,
    逐条下载 PDF 核发布日期, 存了 23+ 个文件、跑了 40 分钟没完。
    本类锁住"预筛器必须自己完成预筛"这条不变量。
    """

    def setUp(self):
        import parse_eping
        self.p = parse_eping
        self.tmp = tempfile.mkdtemp(prefix="radar-eping-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _write(self, name, content):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def _read_filtered(self, path):
        """读脚本产出的 <数据>.filtered.json。"""
        out = path.rsplit(".", 1)[0] + ".filtered.json"
        with open(out, encoding="utf-8") as fh:
            return json.load(fh)

    def _run(self, path, **kw):
        """跑一次脚本, 返回 (退出码, stdout)。"""
        argv = ["parse_eping.py", path, "--date", "2026-10-22", "--days", "7"]
        for k, v in kw.items():
            argv += ["--" + k] if v is True else ["--" + k, str(v)]
        old = sys.argv
        sys.argv = argv
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                code = self.p.main()
        finally:
            sys.argv = old
        return code, buf.getvalue()

    # ---- 直链优先级: 必须取 WTO 存档 PDF, 不能取成员国的附件下载页 ----
    def test_pdf_prefers_wto_archive_over_member_attachment_page(self):
        rec = {"crn": "<a href='https://www.ica.gov.co/getattachment/7b0dc/2026R00020296.aspx'>doc</a>, "
                      "<a href='https://members.wto.org/crnattachments/2026/SPS/COL/26_05465_00_s.pdf'>doc</a>"}
        url = self.p._pdf(rec)
        self.assertEqual(url, "https://members.wto.org/crnattachments/2026/SPS/COL/26_05465_00_s.pdf")

    def test_pdf_falls_back_to_any_pdf_then_attachment_page(self):
        self.assertEqual(self.p._pdf({"crn": "<a href='https://x.gov/a/b.pdf'>d</a>"}),
                         "https://x.gov/a/b.pdf")
        self.assertEqual(self.p._pdf({"crn": "<a href='https://x.gov/getattachment/1/a.aspx'>d</a>"}),
                         "https://x.gov/getattachment/1/a.aspx")
        self.assertEqual(self.p._pdf({"crn": "no link here"}), "")

    # ---- 动作预筛: 纯通知类要排除, 多语言标题要命中 ----
    def test_comment_period_only_is_not_an_action(self):
        self.assertFalse(self.p._is_action({"kw": ["Comment", "Consultation"],
                                            "title": "comment period notice"}))
        # 硬动作词优先于否定词
        self.assertTrue(self.p._is_action({"kw": ["Comment", "Withdrawal of the measure"],
                                           "title": "comment period notice"}))

    def test_accented_non_english_titles_are_matched(self):
        # 西语带音标: 不折叠音标就漏检(10-22 预筛的隐性漏检点)
        self.assertTrue(self.p._is_action(
            {"kw": [], "title": "Resolucion ICA levanta la medida temporal de suspension"}))
        # 葡语: fitossanitarios / requisitos
        self.assertTrue(self.p._is_action(
            {"kw": [], "title": "Requisitos fitossanitarios para importacao"}))
        # 法语
        self.assertTrue(self.p._is_action(
            {"kw": [], "title": "Revision des mesures d'importation"}))
        # 无关标题不误报
        self.assertFalse(self.p._is_action({"kw": [], "title": "Notification of the annual report"}))

    # ---- 日期窗口 ----
    def test_dist_normalizes_iso_and_slash_formats(self):
        self.assertEqual(self.p._dist({"dist": "2026-10-21"}), "2026-10-21")
        self.assertEqual(self.p._dist({"dist": "21/10/2026"}), "2026-10-21")
        self.assertEqual(self.p._dist({"dist": "2026-10-21T08:00:00Z"}), "2026-10-21")
        self.assertIsNone(self.p._dist({"dist": "not a date"}))
        self.assertIsNone(self.p._dist({}))

    def test_out_of_window_rows_are_dropped(self):
        rows = [
            {"id": "1", "sym": "G/SPS/N/A/1", "dist": "2026-10-21", "member": "A",
             "title": "import ban on fresh beef", "kw": ["Import ban"],
             "crn": "<a href='https://members.wto.org/crnattachments/x.pdf'>d</a>"},
            {"id": "2", "sym": "G/SPS/N/B/2", "dist": "2026-09-01", "member": "B",
             "title": "old notification", "kw": ["Comment"],
             "crn": "<a href='https://members.wto.org/crnattachments/y.pdf'>d</a>"},
            {"id": "3", "sym": "G/SPS/N/C/3", "dist": "2026-10-20", "member": "C",
             "title": "comment period notice", "kw": ["Comment", "Consultation"],
             "crn": "<a href='https://members.wto.org/crnattachments/z.pdf'>d</a>"},
        ]
        path = self._write("rows.json", json.dumps(rows, ensure_ascii=False))
        code, out = self._run(path)
        self.assertEqual(code, 0)
        self.assertIn("共解析 3 条", out)
        self.assertIn("窗口(2026-10-15 ~ 2026-10-22)内 2 条", out)
        self.assertIn("疑似措施变化 1 条", out)
        self.assertIn("G/SPS/N/A/1", out)
        self.assertNotIn("G/SPS/N/B/2", out)
        self.assertNotIn("G/SPS/N/C/3", out)

    # ---- 行式文本路径(10-21 的 sps_list.txt 形状) ----
    def test_text_rows_parse_url_and_filter_by_window(self):
        # 5 段格式(无关键词列): 直链必须落到 url 而不是被当成关键词
        text = (
            "Colombia | 2026-10-21 | G/SPS/N/COL/396/Add.1 | Revisa de requisitos | "
            "https://members.wto.org/crnattachments/2026/SPS/COL/26_05465_00_s.pdf\n"
            "Oldland | 2026-09-01 | G/SPS/N/X/1 | muy antiguo | https://x/old.pdf\n"
        )
        path = self._write("rows.txt", text)
        code, out = self._run(path)
        self.assertEqual(code, 0)
        self.assertIn("共解析 2 条", out)
        self.assertIn("疑似措施变化 1 条", out)
        self.assertIn("crnattachments", out)
        filtered = self._read_filtered(path)
        self.assertEqual(len(filtered), 1)
        self.assertIn("crnattachments", filtered[0]["url"])

    def test_text_rows_split_keyword_and_url_columns(self):
        # 6 段格式(多一段关键词): 关键词列会并入标题, 动作判定照样命中
        # (生产真实形状是 5 段, 这里覆盖"多一列也不会漏判")
        text = ("Nowland | 2026-10-20 | G/SPS/N/Y/2 | titulo | Import ban | "
                "https://members.wto.org/crnattachments/2026/SPS/Y/1.pdf\n")
        path = self._write("rows2.txt", text)
        code, out = self._run(path)
        self.assertEqual(code, 0)
        self.assertIn("疑似措施变化 1 条", out)
        filtered = self._read_filtered(path)
        self.assertEqual(len(filtered), 1)
        self.assertIn("Import ban", filtered[0]["title"])
        self.assertIn("crnattachments", filtered[0]["url"])

    # ---- 生产真实形状(2026-10-22 实机验证才发现) ----
    def test_real_production_row_shape_is_parsed(self):
        """10-21 真实产物 sps_list.txt 的行形状。

        真实行 = 序号 | 成员 | 日期 | 文号+标题 | 直链
        (行首有**序号列**、**文号与标题合并在一段**、日期段可能有两个日期)
        —— 与旧文档描述的 5 段格式不同, 旧实现按固定下标解析会全军覆没。
        """
        text = (
            "0 | Colombia | 21/10/2026 | G/SPS/N/COL/396/Add.1 Resolucion ICA 00020296, "
            "Por la cual se levanta la medida temporal de suspension | "
            "https://members.wto.org/crnattachments/2026/SPS/COL/26_05465_00_s.pdf\n"
            "2 | Costa Rica | 20/10/2026 19/12/2026 | G/SPS/N/CRI/371 Proyecto de Resolucion "
            "N.o 025-2026-CV-ARP-SFE. El Servicio Fitosanitario del Estado, Departamento de "
            "Cuarentena Vegetal | "
            "https://members.wto.org/crnattachments/2026/SPS/CRI/26_05437_00_s.pdf\n"
        )
        path = self._write("real.txt", text)
        code, out = self._run(path)
        self.assertEqual(code, 0)
        self.assertIn("共解析 2 条", out)
        self.assertIn("窗口(2026-10-15 ~ 2026-10-22)内 2 条", out)
        filtered = self._read_filtered(path)
        self.assertEqual(len(filtered), 2)
        # 成员不能是序号
        self.assertEqual(filtered[0]["member"], "Colombia")
        # 文号与标题各归其位
        self.assertEqual(filtered[0]["sym"], "G/SPS/N/COL/396/Add.1")
        self.assertIn("levanta la medida", filtered[0]["title"])
        # 日期: 第一个是发布日, 两个日期都要留住(第二个通常是生效日)
        self.assertEqual(filtered[0]["dist"], "2026-10-21")
        self.assertEqual(filtered[0]["dates"], ["21/10/2026"])
        self.assertEqual(filtered[1]["member"], "Costa Rica")
        self.assertEqual(filtered[1]["dist"], "2026-10-20")
        self.assertEqual(filtered[1]["dates"], ["20/10/2026", "19/12/2026"])
        # 直链
        self.assertIn("crnattachments", filtered[0]["url"])

    def test_real_shape_member_is_not_the_index(self):
        """序号列不能被当成成员(否则整批的 country 都错, 入库就全废)。"""
        text = ("0 | Colombia | 21/10/2026 | G/SPS/N/COL/1 levy the temporary suspension "
                "measure | https://x/a.pdf\n"
                "1 | Peru | 20/10/2026 | G/SPS/N/PER/2 phytosanitary requirements for import "
                "| https://x/b.pdf\n")
        path = self._write("idx.txt", text)
        code, out = self._run(path)
        self.assertEqual(code, 0)
        filtered = self._read_filtered(path)
        self.assertEqual([r["member"] for r in filtered], ["Colombia", "Peru"])

    # ---- 业务时区: 默认日期走 biz_today(东八区), 不依赖宿主机 TZ ----
    def test_default_date_uses_business_timezone(self):
        import normalize
        self.assertEqual(self.p.biz_today(), normalize.biz_today())

    # ---- 失败路径 ----
    def test_missing_file_returns_2(self):
        code, out = self._run(os.path.join(self.tmp, "nope.json"))
        self.assertEqual(code, 2)
        self.assertIn("文件不存在", out)

    def test_bad_date_returns_2(self):
        path = self._write("rows.json", "[]")
        old = sys.argv
        sys.argv = ["parse_eping.py", path, "--date", "2026-13-45"]
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                code = self.p.main()
        finally:
            sys.argv = old
        self.assertEqual(code, 2)
        self.assertIn("YYYY-MM-DD", buf.getvalue())

    def test_empty_input_returns_1(self):
        path = self._write("empty.json", "[]")
        code, out = self._run(path)
        self.assertEqual(code, 1)
        self.assertIn("没有解析出条目", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
