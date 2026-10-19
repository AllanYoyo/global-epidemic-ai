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

sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "templates"))


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

    def test_third_party_host_rejected(self):
        e = _source_event(
            source={"tier": 1, "name": "n",
                    "url": "https://freyrsolutions.com/notice/1", "quote": "q"},
            verification_status="unverified")
        errs = self.n.validate(dict(e))
        self.assertTrue(any("非官方站点" in m for m in errs), errs)

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


class PushReportReviewGateTests(unittest.TestCase):
    """评审核闸门(2026-10-19 引入): 用户在微信/钉钉端提测试请求时, 反馈必须在原渠道完成;
    评审核闸门强制: REVIEW_CHANNEL=wechat 模式下, 首跑只发微信 + 写 .review-pending,
    --review-approved 才把剩余渠道发完并清理待办。

    复现 2026-10-19: 用户强调"先微信审核", 但 push_report.py 旧版一发就发齐
    (WECHAT_WEBHOOK + DINGTALK_WEBHOOK 同时配就同时发), 不分阶段。
    """

    def setUp(self):
        import push_report
        self.pr = push_report
        self.tmp = tempfile.mkdtemp(prefix="push-gate-")
        # push_report 在 import 时已经从 normalize 锁定 DATA_DIR, 模块级常量
        # PUSH_FLAG_DIR / PENDING_DIR 也已基于旧 DATA_DIR 解析。这里把它们
        # 重新指向当前测试 tmp 目录, 让每个测试用独立的 flag/pending 状态。
        self.flag_dir = os.path.join(self.tmp, "reports", ".pushed")
        self.pending_dir = os.path.join(self.tmp, "reports", ".review-pending")
        self.env_backup = dict(os.environ)
        os.environ["WECHAT_WEBHOOK"] = "https://test.example/wechat"
        os.environ["DINGTALK_WEBHOOK"] = "https://test.example/dingtalk"
        os.environ.pop("REVIEW_CHANNEL", None)
        os.environ.pop("SMTP_HOST", None)

        self.calls = []
        self._patches = [
            mock.patch.object(self.pr, "PUSH_FLAG_DIR", self.flag_dir),
            mock.patch.object(self.pr, "PENDING_DIR", self.pending_dir),
            mock.patch.object(self.pr, "send_wechat",
                              side_effect=lambda t: self.calls.append(("wechat", t))),
            mock.patch.object(self.pr, "send_dingtalk",
                              side_effect=lambda t, title: self.calls.append(("dingtalk", t, title))),
            mock.patch.object(self.pr, "send_mail",
                              side_effect=lambda s, t, p: self.calls.append(("mail", s, t, p))),
            mock.patch.object(self.pr.report, "collect",
                              return_value={"new": [], "active": [], "covered": []}),
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
        """按指定 args + env 跑 push_report.main(), 收集 stdout。"""
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

    def test_backward_compat_no_review_channel_sends_all(self):
        # 默认(无 REVIEW_CHANNEL): 所有配置渠道一次性发出, 写 .pushed
        rc, out = self._run(["--date", "2026-10-19"])
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
        # REVIEW_CHANNEL 设了非法值 → 退化为全量(向后兼容)
        rc, out = self._run(["--date", "2026-10-19"],
                            env_extra={"REVIEW_CHANNEL": "unknown-channel"})
        self.assertEqual(rc, 0, out)
        channels_called = {c[0] for c in self.calls}
        self.assertEqual(channels_called, {"wechat", "dingtalk"}, out)

    def test_review_channel_mail_without_smtp_errors(self):
        # REVIEW_CHANNEL=mail 但 SMTP_HOST 未配 → 报错, 不发任何渠道
        os.environ.pop("SMTP_HOST", None)
        rc, out = self._run(["--date", "2026-10-19"],
                            env_extra={"REVIEW_CHANNEL": "mail"})
        self.assertNotEqual(rc, 0)
        self.assertIn("REVIEW_CHANNEL=mail", out)
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

    def test_message_path_bypasses_review_gate(self):
        # --message 走自定义文本, 不参与评审核闸门(避免 cron fallback 通知被卡)
        rc, out = self._run(["--date", "2026-10-19",
                             "--message", "⏰ 提醒"],
                            env_extra={"REVIEW_CHANNEL": "wechat"})
        self.assertEqual(rc, 0, out)
        channels_called = {c[0] for c in self.calls}
        self.assertEqual(channels_called, {"wechat", "dingtalk"}, out)
        # --message 不写 .review-pending, 也不写 .pushed
        self.assertFalse(os.path.isfile(
            os.path.join(self.tmp, "reports", ".review-pending", "2026-10-19.flag")))
        self.assertFalse(os.path.isfile(
            os.path.join(self.tmp, "reports", ".pushed", "2026-10-19.flag")))


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
