#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""疫见全球 · 最小回归自测(stdlib only, 无 pytest 依赖)。

覆盖两个 P1 修复后的关键合同不变量:
- templates/base.py: openpyxl 不再模块级导入,样式函数在缺包时按需抛 ImportError
- scripts/normalize.py: event_id 关联对象必填,杜绝同 country/domain/action/date 的两条政策碰撞合并
- scripts/normalize.py: 合并保留 event_id / first_seen
- scripts/normalize.py: 旧闻拦截(发布时间早于 --max-age-days 窗口的新记录拒收,
  已入库 event_id 的合并更新不受影响, 0 关闭)
- scripts/risk.py: 四维加权合同(score / level / focus 三者一致, 0.05 容差)

用法:
  python scripts/test_smoke.py            # 简洁输出
  python scripts/test_smoke.py -v         # 详细输出
退出码: 全部通过 -> 0, 任一失败 -> 1。
"""
import datetime
import io
import json
import os
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

    def test_known_third_party_hosts_flagged(self):
        for host in ("bangladeshtradeportal.gov.bd", "thebrief.com.na",
                     "agrinfo.eu", "freyrsolutions.com", "www.evs.ee"):
            self.assertTrue(self.n._is_third_party(host), host)


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
    """替 normalize.datetime: 固定 today/now, 让时间窗测试与真实时钟解耦。"""

    timedelta = datetime.timedelta

    class date:
        @staticmethod
        def today():
            return datetime.date(2026, 10, 12)

    class datetime:
        @staticmethod
        def now():
            return datetime.datetime(2026, 10, 12, 8, 0, 0)


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
