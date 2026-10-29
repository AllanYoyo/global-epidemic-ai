#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""urlwatch 取数内核的离线测试(stdlib unittest, 不联网, 无 pytest 依赖)

对照方案「urlwatch 取数层重写方案 · 疫见全球」的验收用例:
  A1  服务器回 304(带正确 ETag) → unchanged, 不落盘, 不进 LLM, etag 更新
  A2  200 但仅导航栏/日期戳变化 → 关注面 sha1 未变 → unchanged, 不落盘
  A3  200 且正文新增一条政策 → changed, 落盘 1 份, diff 含新增行
  A4  某源连接失败 → 该源 error, 其余源照常完成
  A5  全部源失败 → fetch-status.json 的 infra_failure=true(不得推送)
  A6  全部源未变 → infra_failure=false, archives=0(安静日合法)
  A7  单源候选文章数 > 2 → 落盘条数 ≤2, 配额口径 = collect.count_archives
  A8  归档目录日期 = 东八区当日(biz_now), 不是 date.today()
  A9  落盘格式 → *.meta.json 字段集与 collect.py 契约一致, 下游可直接消费
  A10 条件请求逃生开关 → --no-conditional 时忽略 304 强制重取
  10.3 对照验收 → 同一批 fixture, 新内核候选 URL 集合 == 旧 fetch_pages.py

用法:
  python scripts/test_fetchlib.py          # 全部通过 -> 0
  python scripts/test_fetchlib.py -v       # 详细输出
"""
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
FIXTURES = os.path.join(HERE, "tests", "fixtures")

# 测试环境隔离: 把 data/ database/ 指到 tmp, 避免污染真实库与目录
TMP = tempfile.mkdtemp(prefix="radar-fetchlib-")
os.environ["EPIDEMIC_DATA_DIR"] = TMP
os.environ["EPIDEMIC_DB"] = os.path.join(TMP, "smoke.db")
os.environ["EPIDEMIC_FETCH_DB"] = os.path.join(TMP, "fetch.db")

sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "templates"))

import policy_fetch                                        # noqa: E402
import fetch_pages                                         # noqa: E402
import collect as collect_mod                              # noqa: E402
from fetchlib import runner, filters                       # noqa: E402
from fetchlib.cache import SQLiteCacheStorage              # noqa: E402
from fetchlib.jobbase import NotModifiedError              # noqa: E402
from fetchlib.urljob import (UrlJob, HttpStatusError,      # noqa: E402
                              TransportError)

LIST_URL = "https://example.org/list"
ARTICLE_URL = "https://example.org/noticias/2026-10-28-nueva-medida"
ARTICLE2_URL = "https://example.org/comunicados/sanidad-importacion-pulgas"
FAKE_SOURCES = [("priority_1", "测试国", "Testland", "test-src", LIST_URL)]


def fixture(name):
    with open(os.path.join(FIXTURES, name), "rb") as f:
        return f.read()


def make_transport(pages, etag='"v1"', fail_urls=()):
    """假传输层: 按 URL 返回固定内容; 带 If-None-Match 时回 304。

    pages: {url: bytes}
    fail_urls: 这些 URL 直接抛 TransportError(模拟连接失败)
    """
    state = {'seen': [], 'headers': []}

    def transport(url, headers=None, timeout=None):
        state['seen'].append(url)
        state['headers'].append(dict(headers or {}))
        if url in fail_urls:
            raise TransportError('%s: 连接失败' % url)
        if headers and headers.get('If-None-Match') and etag:
            return 304, b'', {'ETag': etag}
        body = pages.get(url)
        if body is None:
            raise TransportError('%s: 无 fixture' % url)
        return 200, body, {'ETag': etag}

    transport.state = state
    return transport


class FetchLibKernelTests(unittest.TestCase):
    """内核自身的行为。"""

    def setUp(self):
        self.cache = SQLiteCacheStorage(os.path.join(TMP, "kernel-%s.db" % id(self)))
        self.transport = make_transport({
            LIST_URL: fixture("entry_list.html"),
            ARTICLE_URL: fixture("article_policy.html"),
        })

    def tearDown(self):
        self.cache.close()

    def test_plugin_self_registration(self):
        # __kind__ 即 YAML 配置键: 类一创建就注册进基类字典, 无需改中心注册表
        from fetchlib.filters import FilterBase
        from fetchlib.urljob import UrlJob as _UrlJob
        self.assertIn('html2text', FilterBase.__subclasses__)
        self.assertIn('sha1sum', FilterBase.__subclasses__)
        self.assertIn('url', _UrlJob.__subclasses__)

    def test_required_field_missing_raises(self):
        with self.assertRaises(ValueError):
            UrlJob(transport=self.transport)          # 缺 url

    def test_attention_fingerprint_ignores_nav(self):
        """A2 核心: 导航/页脚/日期戳变了, 关注面指纹不变。"""
        chain = policy_fetch.DEFAULT_ATTENTION_FILTER
        f1 = runner.fingerprint(fixture("entry_list.html").decode('utf-8'), chain)
        f2 = runner.fingerprint(fixture("entry_list_nav_changed.html").decode('utf-8'), chain)
        self.assertEqual(f1, f2, "导航/日期戳变化不应改变关注面指纹")

    def test_attention_fingerprint_changes_on_new_policy(self):
        chain = policy_fetch.DEFAULT_ATTENTION_FILTER
        f1 = runner.fingerprint(fixture("article_policy.html").decode('utf-8'), chain)
        f2 = runner.fingerprint(fixture("article_policy_v2.html").decode('utf-8'), chain)
        self.assertNotEqual(f1, f2, "正文新增政策必须改变关注面指纹")

    def test_html_to_text_excludes_noise(self):
        text = filters.html_to_text(fixture("entry_list.html").decode('utf-8'))
        self.assertIn("Nueva medida sanitaria", text)
        self.assertNotIn("Pie de pagina", text)
        self.assertNotIn("Todos los derechos", text)

    def test_conditional_headers_sent_and_cleared_on_retry(self):
        """条件请求头: 有 etag 时发 If-None-Match; 重试(tries>0)时撤销。"""
        job = UrlJob(url=LIST_URL, transport=self.transport)
        state = runner.JobState(self.cache, LIST_URL)
        state.etag = '"v1"'
        job.build_headers(state)
        self.assertEqual(job.build_headers(state).get('If-None-Match'), '"v1"')
        state.tries = 1
        headers = job.build_headers(state)
        self.assertNotIn('If-None-Match', headers)
        self.assertNotIn('If-Modified-Since', headers)

    def test_custom_headers_override_case_insensitively(self):
        job = UrlJob(url=LIST_URL, transport=self.transport, headers={'User-Agent': 'X'})
        headers = job.build_headers(runner.JobState(self.cache, LIST_URL))
        self.assertEqual(headers['User-Agent'], 'X')

    def test_304_raises_not_modified(self):
        job = UrlJob(url=LIST_URL, transport=self.transport)
        state = runner.JobState(self.cache, LIST_URL)
        state.etag = '"v1"'
        with self.assertRaises(NotModifiedError):
            job.retrieve(state)

    def test_encoding_fallback_latin1(self):
        """编码回退链: 非 utf-8 字节走 latin1, 不炸。"""
        job = UrlJob(url=LIST_URL, transport=self.transport)
        raw = "Resolución".encode('latin1')
        self.assertIn("Resol", job.decode(raw, {}))

    def test_cache_roundtrip(self):
        self.cache.save_state(ARTICLE_URL, source_id='s1', etag='"e"',
                              content_sha1='abc', last_status=200, last_fetched=1.0)
        st = self.cache.load_state(ARTICLE_URL)
        self.assertEqual(st['etag'], '"e"')
        self.assertEqual(st['content_sha1'], 'abc')
        self.cache.save_body(ARTICLE_URL, 'abc', '正文', 2.0)
        body, sha1, ts = self.cache.load_body(ARTICLE_URL)
        self.assertEqual((body, sha1, ts), ('正文', 'abc', 2.0))


class JudgeTests(unittest.TestCase):
    """四态判定(A1/A2/A3)。"""

    def setUp(self):
        self.cache = SQLiteCacheStorage(os.path.join(TMP, "judge-%s.db" % id(self)))

    def tearDown(self):
        self.cache.close()

    def _judge(self, transport, url, chain=None):
        job = UrlJob(url=url, transport=transport)
        return runner.judge_url(self.cache, url, policy_fetch.make_fetch_fn(),
                                attention_filter=chain or policy_fetch.DEFAULT_ATTENTION_FILTER,
                                job=job)

    def test_a1_304_unchanged_no_archive(self):
        """A1: 条件请求命中 → unchanged, 不落盘, etag 更新。"""
        transport = make_transport({LIST_URL: fixture("entry_list.html")})
        job = UrlJob(url=LIST_URL, transport=transport)
        fetch_fn = policy_fetch.make_fetch_fn()
        r1 = runner.judge_url(self.cache, LIST_URL, fetch_fn,
                              attention_filter=policy_fetch.DEFAULT_ATTENTION_FILTER, job=job)
        self.assertEqual(r1.status, runner.STATUS_NEW)
        r2 = runner.judge_url(self.cache, LIST_URL, fetch_fn,
                              attention_filter=policy_fetch.DEFAULT_ATTENTION_FILTER, job=job)
        self.assertEqual(r2.status, runner.STATUS_UNCHANGED, "第二轮应命中 304 → unchanged")
        self.assertEqual(r2.http_status, 304)
        st = self.cache.load_state(LIST_URL)
        self.assertEqual(st['etag'], '"v1"', "etag 必须回写供下一轮条件请求")

    def test_a2_nav_only_change_unchanged(self):
        """A2: 仅导航/日期戳变化 → 关注面未变 → unchanged。"""
        transport = make_transport({LIST_URL: fixture("entry_list_nav_changed.html")})
        job = UrlJob(url=LIST_URL, transport=transport)
        fetch_fn = policy_fetch.make_fetch_fn()
        r1 = runner.judge_url(self.cache, LIST_URL, fetch_fn,
                              attention_filter=policy_fetch.DEFAULT_ATTENTION_FILTER, job=job)
        self.assertEqual(r1.status, runner.STATUS_NEW)
        # 换成只改了导航的版本, 且不提供 etag(模拟服务器没给条件凭据)
        transport2 = make_transport({LIST_URL: fixture("entry_list_nav_changed.html")}, etag=None)
        job2 = UrlJob(url=LIST_URL, transport=transport2)
        r2 = runner.judge_url(self.cache, LIST_URL, policy_fetch.make_fetch_fn(),
                              attention_filter=policy_fetch.DEFAULT_ATTENTION_FILTER, job=job2)
        self.assertEqual(r2.status, runner.STATUS_UNCHANGED, "导航变化不应判为 changed")

    def test_a3_new_policy_changed_with_diff(self):
        """A3: 正文新增一条政策 → changed, diff 含新增行。"""
        transport = make_transport({ARTICLE_URL: fixture("article_policy.html")}, etag=None)
        job = UrlJob(url=ARTICLE_URL, transport=transport)
        fetch_fn = policy_fetch.make_fetch_fn()
        r1 = runner.judge_url(self.cache, ARTICLE_URL, fetch_fn,
                              attention_filter=policy_fetch.DEFAULT_ATTENTION_FILTER, job=job)
        self.assertEqual(r1.status, runner.STATUS_NEW)
        self.assertIn("Nueva medida sanitaria", r1.body)

        transport2 = make_transport({ARTICLE_URL: fixture("article_policy_v2.html")}, etag=None)
        job2 = UrlJob(url=ARTICLE_URL, transport=transport2)
        r2 = runner.judge_url(self.cache, ARTICLE_URL, policy_fetch.make_fetch_fn(),
                              attention_filter=policy_fetch.DEFAULT_ATTENTION_FILTER, job=job2)
        self.assertEqual(r2.status, runner.STATUS_CHANGED)
        self.assertIn("NUEVA_POLITICA", r2.diff, "diff 必须含新增行")

    def test_error_status_isolated(self):
        transport = make_transport({}, fail_urls=(LIST_URL,))
        job = UrlJob(url=LIST_URL, transport=transport)
        res = runner.judge_url(self.cache, LIST_URL, policy_fetch.make_fetch_fn(),
                               attention_filter=policy_fetch.DEFAULT_ATTENTION_FILTER, job=job)
        self.assertEqual(res.status, runner.STATUS_ERROR)

    def test_run_parallel_isolates_exceptions(self):
        """单项异常不得中断整轮(urlwatch 上游会 raise, 这里刻意偏离)。"""
        def boom(x):
            if x == 2:
                raise RuntimeError("boom")
            return x

        out = runner.run_parallel(boom, [1, 2, 3])
        self.assertEqual(len(out), 3)
        self.assertIn(1, out)
        self.assertIn(3, out)


class PolicyFetchTests(unittest.TestCase):
    """领域层(A4-A10 + 10.3 对照)。"""

    def setUp(self):
        # 每个用例独立的数据目录与抓取状态库 —— 否则前一个用例留下的状态
        # 会让后一个用例判成「未变」(缓存跨用例污染是这类测试最常见的假绿)
        self.date = "2026-10-29"
        self.tmp = tempfile.mkdtemp(prefix="radar-pf-")
        self._old_data_dir = policy_fetch.DATA_DIR
        policy_fetch.DATA_DIR = self.tmp
        os.environ["EPIDEMIC_DATA_DIR"] = self.tmp
        os.environ["EPIDEMIC_FETCH_DB"] = os.path.join(self.tmp, "fetch.db")

        self.pages = {
            LIST_URL: fixture("entry_list.html"),
            ARTICLE_URL: fixture("article_policy.html"),
            ARTICLE2_URL: fixture("article_policy_v2.html"),
        }
        self.raw_dir = os.path.join(self.tmp, "raw", self.date)

    def tearDown(self):
        policy_fetch.DATA_DIR = self._old_data_dir
        os.environ["EPIDEMIC_DATA_DIR"] = TMP
        os.environ["EPIDEMIC_FETCH_DB"] = os.path.join(TMP, "fetch.db")
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, transport, sources=None, **kwargs):
        sources = sources if sources is not None else FAKE_SOURCES
        probe = [(s[3], 200, "ok", "") for s in sources]
        with mock.patch.object(policy_fetch.scan_targets, "_pick", return_value=sources), \
             mock.patch.object(policy_fetch.probe_channels, "probe", return_value=probe):
            return policy_fetch.run(self.date, transport=transport, **kwargs)

    def _archives(self):
        if not os.path.isdir(self.raw_dir):
            return []
        return [f for f in os.listdir(self.raw_dir) if f.endswith('.meta.json')]

    def _read_json(self, path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def test_a4_single_source_failure_does_not_stop_others(self):
        """A4: 一个源失败, 另一个源照常完成。"""
        bad = ("priority_1", "坏国", "Badland", "bad-src", "https://bad.example.org/list")
        sources = FAKE_SOURCES + [bad]
        transport = make_transport(self.pages, fail_urls=("https://bad.example.org/list",))
        summary, _logs = self._run(transport, sources=sources)
        self.assertEqual(summary['sources_dead'], 1)
        self.assertGreaterEqual(summary['sources_alive'], 1)
        self.assertFalse(summary['infra_failure'])
        self.assertGreater(summary['archives'], 0, "好源仍应产出存档")

    def test_a5_all_sources_failed_infra_failure(self):
        """A5: 全部源失败 → infra_failure=true(判基础设施故障, 不得推送)。"""
        transport = make_transport(self.pages, fail_urls=tuple(self.pages.keys()))
        summary, _logs = self._run(transport)
        self.assertTrue(summary['infra_failure'])
        self.assertEqual(summary['sources_alive'], 0)
        status = self._read_json(os.path.join(self.raw_dir, "fetch-status.json"))
        self.assertTrue(status['infra_failure'])

    def test_a6_all_unchanged_quiet_day(self):
        """A6: 全部源未变 → 不算故障, archives=0。"""
        transport = make_transport(self.pages)
        # 先跑一轮建状态, 再跑第二轮 → 全部未变
        self._run(transport)
        summary, _logs = self._run(transport)
        self.assertFalse(summary['infra_failure'])
        self.assertEqual(summary['archives'], 0)

    def test_a7_per_source_cap(self):
        """A7: 单源 ≤2 条(防洪水位), 配额口径 = collect.count_archives。"""
        transport = make_transport(self.pages)
        summary, _logs = self._run(transport, max_per_source=2)
        self.assertLessEqual(summary['archives'], 2)
        self.assertEqual(len(self._archives()), summary['archives'])
        self.assertEqual(collect_mod.count_archives(self.raw_dir), summary['archives'])

    def test_a8_archive_dir_uses_biz_date(self):
        """A8: 归档目录用东八区当日, 不是 date.today()。"""
        transport = make_transport(self.pages)
        self._run(transport)
        self.assertTrue(os.path.isdir(self.raw_dir))
        self.assertEqual(os.path.basename(self.raw_dir), self.date)

    def test_a9_meta_contract_unchanged(self):
        """A9: *.meta.json 字段集与 collect.py 契约一致, 下游可直接消费。"""
        transport = make_transport(self.pages)
        self._run(transport)
        metas = self._archives()
        self.assertTrue(metas, "应至少产出一份存档")
        meta = self._read_json(os.path.join(self.raw_dir, metas[0]))
        for key in ('title', 'url', 'file', 'local_source_file', 'fetched_at', 'tool'):
            self.assertIn(key, meta, "meta 契约字段缺失: %s" % key)
        self.assertEqual(meta['tool'], "collect.py")
        body_path = os.path.join(self.raw_dir, meta['file'])
        self.assertTrue(os.path.isfile(body_path), "正文文件必须存在")

    def test_a10_no_conditional_forces_refetch(self):
        """A10: --no-conditional 时不带条件头重新取正文(强制重取)。

        注意: 入口页关注面未变时整源跳过(这是 D5 的设计), 所以要让文章重新
        被取到, 列表页本身也要有变化 —— 真实场景里新增公告本来就会改列表页。
        """
        transport = make_transport(self.pages)
        self._run(transport)
        # 第二轮: 列表新增一条 + 文章正文变化 + 关闭条件请求
        pages = dict(self.pages)
        pages[LIST_URL] = fixture("entry_list_new_article.html")
        pages[ARTICLE_URL] = fixture("article_policy_v2.html")
        pages["https://example.org/noticias/2026-10-29-nueva-suspension"] = \
            fixture("article_policy_v2.html")
        transport2 = make_transport(pages)
        summary, _logs = self._run(transport2, conditional=False)
        self.assertGreater(summary['archives'], 0, "关闭条件请求后应重新存档")
        sent = [h for h in transport2.state['headers'] if h.get('If-None-Match')]
        self.assertEqual(sent, [], "关闭条件请求后不得再发 If-None-Match")

    def test_entry_page_unchanged_skips_source(self):
        """入口页关注面未变 → 整源跳过(不重复取文章页, 不重复存档)。"""
        transport = make_transport(self.pages)
        self._run(transport)
        first = len(self._archives())
        transport2 = make_transport(self.pages)
        summary, _logs = self._run(transport2)
        self.assertEqual(summary['archives'], 0, "入口页未变不应重复存档")
        self.assertEqual(len(self._archives()), first)
        article_hits = [u for u in transport2.state['seen'] if u == ARTICLE_URL]
        self.assertEqual(article_hits, [], "入口页未变时不应再取文章页")

    def test_parity_with_legacy_candidate_set(self):
        """10.3: 同一批 fixture, 新内核候选 URL 集合 == 旧 fetch_pages.py。"""
        import datetime
        today = datetime.date.fromisoformat(self.date)
        html = fixture("entry_list.html").decode('utf-8')
        links = fetch_pages.extract_links(html, LIST_URL)
        legacy_confirmed, legacy_to_verify = fetch_pages.pick_candidates(links, LIST_URL, today)

        def fake_curl(url, timeout=15, max_bytes=400000):
            if url == ARTICLE2_URL:
                return 200, fixture("article_policy_v2.html").decode('utf-8')
            return 0, ""

        with mock.patch.object(fetch_pages, "curl", side_effect=fake_curl):
            legacy_verified = fetch_pages.verify_dates(legacy_to_verify, today)
        legacy_urls = ({u for u, _t, _d in legacy_confirmed}
                       | {u for u, _t, _d in legacy_verified})
        self.assertTrue(legacy_urls, "fixture 应产出候选")

        transport = make_transport(self.pages)
        self._run(transport)
        cand_path = os.path.join(self.raw_dir, "candidates.jsonl")
        new_urls = set()
        if os.path.isfile(cand_path):
            with open(cand_path, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        new_urls.add(json.loads(line)['url'])
        self.assertTrue(new_urls, "新内核应产出候选")
        self.assertTrue(new_urls.issubset(legacy_urls),
                        "新内核候选必须是旧取数器候选的子集: 新=%s 旧=%s"
                        % (sorted(new_urls), sorted(legacy_urls)))

    def test_fetch_status_file_written(self):
        transport = make_transport(self.pages)
        self._run(transport)
        path = os.path.join(self.raw_dir, "fetch-status.json")
        self.assertTrue(os.path.isfile(path))
        data = self._read_json(path)
        for key in ('date', 'sources_total', 'sources_alive', 'sources_dead',
                    'sources_changed', 'sources_unchanged', 'archives',
                    'infra_failure', 'per_source'):
            self.assertIn(key, data)
        self.assertTrue(data['per_source'], "per_source 应有逐源明细")


class SourceOptionsTests(unittest.TestCase):
    """逐源关注面链(sources.yaml 的可选 attention_filter)。"""

    def test_parse_attention_filter(self):
        import scan_targets
        self.assertEqual(scan_targets.parse_attention_filter("[html2text, strip, sha1sum]"),
                         ["html2text", "strip", "sha1sum"])
        self.assertIsNone(scan_targets.parse_attention_filter(""))
        self.assertIsNone(scan_targets.parse_attention_filter("[]"))

    def test_parse_sources_recognizes_attention_filter(self):
        import scan_targets
        text = ("sources:\n"
                "  - id: xx\n"
                "    name: XX\n"
                "    url: https://xx.example/\n"
                "    tier: 1\n"
                "    track: policy\n"
                "    attention_filter: [html2text, strip, sha1sum]\n")
        parsed = scan_targets.parse_sources(text)
        self.assertEqual(parsed["xx"]["attention_filter"], "[html2text, strip, sha1sum]")

    def test_known_filter_is_used(self):
        import scan_targets
        stub = {"s1": {"attention_filter": "[html2text, strip, sha1sum]"}}
        with mock.patch.object(scan_targets, "parse_sources", return_value=stub):
            opts = policy_fetch.load_source_options()
        self.assertEqual(opts["s1"]["attention_filter"], ["html2text", "strip", "sha1sum"])

    def test_unknown_filter_warns_and_falls_back(self):
        """未知过滤器名 → 告警 + 回退默认链(配置写了却不生效是最难查的一类坑)。"""
        import scan_targets
        stub = {"s1": {"attention_filter": "[nosuchfilter]"}}
        buf = io.StringIO()
        with mock.patch.object(scan_targets, "parse_sources", return_value=stub), \
             redirect_stdout(buf):
            opts = policy_fetch.load_source_options()
        self.assertNotIn("s1", opts)
        self.assertIn("nosuchfilter", buf.getvalue())


class ProbeChannelsTests(unittest.TestCase):
    """HTTP 传输层改造的回归(http_get 契约不变)。"""

    def test_http_get_full_signature(self):
        import probe_channels
        self.assertTrue(callable(probe_channels.http_get_full))
        self.assertTrue(callable(probe_channels.http_get))

    def test_default_encoding_matches_subprocess_text_mode(self):
        import probe_channels
        self.assertIsInstance(probe_channels._default_encoding(), str)


class ArchiveBodyConcurrencyTests(unittest.TestCase):
    """P1: 跨源并发存档时, 正文不得与 source.url 错配(2026-10-29 修复)。

    旧实现的临时文件名只由**标题**推导(锚文本), 而 fetch_pages 对无锚文本的
    链接统一填「(无标题)」→ 不同源的空锚链接拿到同一个文件名; 写入又发生在锁
    **之外**, 一个线程的重写会截断另一个线程正在被 collect.py 读取的正文 →
    存下来的是「url 指向 A 页、正文却是 B 页」的错配, 破坏「source.url +
    source.quote 必须出自同一原文」的溯源硬要求。
    """

    def test_same_title_concurrent_archives_keep_own_body(self):
        import threading
        import time

        ctx = {
            'tmp_dir': tempfile.mkdtemp(prefix="radar-archive-"),
            'date': "2026-10-29",
            'max_archives_per_day': 0,
            'archive_lock': threading.Lock(),
        }
        seen_body = {}
        seen_path = {}
        real_run = policy_fetch.subprocess.run
        start = threading.Barrier(2)

        def fake_run(cmd, *a, **kw):
            """假装 collect.py: 读出临时文件的内容, 并记下路径。"""
            path = cmd[cmd.index("--file") + 1]
            url = cmd[cmd.index("--url") + 1]
            # 放大"写文件在锁外"的串扰窗口: 本线程读文件期间, 另一线程可能
            # 正把同一个路径 truncate 重写(旧实现就是这个形态)。
            time.sleep(0.5)
            with open(path, encoding="utf-8") as f:
                seen_body[url] = f.read()
            seen_path[url] = path

            class R(object):
                returncode = 0
                stdout = "[OK]"
                stderr = ""
            return R()

        url_a = "https://example.org/a"
        url_b = "https://example.org/b"
        body_a = "A" * 40000
        body_b = "B" * 40000

        try:
            with mock.patch.object(policy_fetch.subprocess, "run", fake_run):
                def work(url, body):
                    start.wait(timeout=20)
                    policy_fetch.archive_body(ctx, url, "(无标题)", body)

                ts = [threading.Thread(target=work, args=(url_a, body_a)),
                      threading.Thread(target=work, args=(url_b, body_b))]
                for t in ts:
                    t.start()
                for t in ts:
                    t.join(timeout=30)
                for t in ts:
                    self.assertFalse(t.is_alive(), "存档线程未结束")

            self.assertEqual(seen_body.get(url_a), body_a,
                             "url_a 的存档正文被别的源覆盖了(溯源链被破坏)")
            self.assertEqual(seen_body.get(url_b), body_b,
                             "url_b 的存档正文被别的源覆盖了(溯源链被破坏)")
            self.assertNotEqual(seen_path.get(url_a), seen_path.get(url_b),
                                 "同名标题必须用不同的临时文件, 否则并发写会互相截断")
        finally:
            shutil.rmtree(ctx['tmp_dir'], ignore_errors=True)


if __name__ == "__main__":
    try:
        result = unittest.main(exit=False, verbosity=2).result
        sys.exit(0 if result.wasSuccessful() else 1)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
