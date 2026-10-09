#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""疫见全球 · 最小回归自测(stdlib only, 无 pytest 依赖)。

覆盖两个 P1 修复后的关键合同不变量:
- templates/base.py: openpyxl 不再模块级导入,样式函数在缺包时按需抛 ImportError
- scripts/normalize.py: event_id 关联对象必填,杜绝同 country/domain/action/date 的两条政策碰撞合并
- scripts/normalize.py: 合并保留 event_id / first_seen
- scripts/risk.py: 四维加权合同(score / level / focus 三者一致, 0.05 容差)

用法:
  python scripts/test_smoke.py            # 简洁输出
  python scripts/test_smoke.py -v         # 详细输出
退出码: 全部通过 -> 0, 任一失败 -> 1。
"""
import os
import sys
import tempfile
import unittest

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
            "tier": 1, "name": "测试源", "url": "https://example.com/p1",
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
