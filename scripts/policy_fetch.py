#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""疫见全球 · urlwatch 内核取数器(2026-10-29 引入)—— 条件请求 + 关注面变更判定

为什么换内核(取代 fetch_pages.py 的取数部分):
  旧取数器每轮**全量重下** 47 个源的入口页与候选文章页, 没有任何条件请求与
  快照比对 —— 页面没变也照下照存, 靠「发布日期在 7 天窗口内」这一个粗筛 +
  LLM 抽取后的 event_id 合并来去重。代价是带宽与 token。
  新内核把 urlwatch 的引擎骨架(作业模型 / 过滤器链 / 缓存抽象 / 状态机 / 四态
  判定)移植进来, 带来两件事:
    ① 条件请求(ETag / If-Modified-Since / 304)—— 上一轮抓过的 URL 未改就不再
       下载正文;
    ② 关注面指纹(过滤链 sha1)—— 导航栏/日期戳/页脚变了不算「变了」, 未变的
       页面不进 LLM 抽取。
  语义新增的判定权仍在 normalize.py 的 event_id 合并 + 非政策闸门, 内核只给
  「哪些 URL 的关注面变了」这条线索 —— 不越权。

不变的部分(刻意):
  · 链接启发式与多语种日期解析 —— 直接复用 fetch_pages.py 的函数, 不另起一套
    (两套口径是本仓库反复出事的病根);
  · 存档落盘与配额口径 —— 直接调用 collect.py, *.meta.json 成对落盘不变;
  · 四层降级与死通道预探 —— 复用 probe_channels.py(降级链已抽成 http_get_full,
    新内核与预探共用同一份实现);
  · 下游 extract_records / normalize / risk / report / push_report —— 一行不改。

产出:
  data/raw/<日期>/NN-slug.html + NN-slug.meta.json   存档(collect.py 落盘)
  data/raw/<日期>/candidates.jsonl                   候选清单
  data/raw/<日期>/search-log.md                      逐源留痕
  data/raw/<日期>/fetch-status.json                  per-source 状态汇总(终检输入)

用法(由 run_scan.sh 调用):
  python3 scripts/policy_fetch.py --date 2026-10-29
  python3 scripts/policy_fetch.py --date 2026-10-29 --no-conditional   # 强制全量重取
  python3 scripts/policy_fetch.py --selftest                            # 离线自检(不联网)
退出码: 0=跑完(哪怕 0 条, 安静日合法); 2=参数/环境错误。
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from normalize import biz_today, DATA_DIR  # 业务时区与数据目录口径
import scan_targets
import probe_channels
import fetch_pages                       # 复用链接启发式与多语种日期解析(单一来源)
import collect as collect_mod            # 存档计数口径与 collect.py 同源
from fetchlib import runner, filters
from fetchlib.cache import SQLiteCacheStorage
from fetchlib.jobbase import NotModifiedError
from fetchlib.urljob import (UrlJob, HttpStatusError, TransportError,
                              DEFAULT_USER_AGENT)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FETCH_TIMEOUT = 15          # 单页 curl 超时(秒), 与 fetch_pages 同口径
MAX_BODY_BYTES = 400000     # 单页正文上限, 与 fetch_pages 同口径
MAX_WORKERS = runner.MAX_WORKERS
DEFAULT_MAX_PER_SOURCE = 2  # 单源最多存档数(真正的防洪水位)
DEFAULT_MAX_TOTAL = 0       # 当日总上限, 0 = 不限(与 fetch_pages 同口径)

# 关注面过滤链: 抽正文 → 去首尾空白 → 取指纹。
# 政府站点的导航/页脚/日期戳每轮都在变, 不压掉就会把 47 个源全判成 changed,
# 涌进 LLM 抽取 → 重演 2026-10-24 token 爆量事故。
DEFAULT_ATTENTION_FILTER = ['html2text', 'strip', 'sha1sum']


def fetch_db_path():
    """抓取状态库路径(独立文件, 不碰 epidemic.db)。"""
    return os.environ.get("EPIDEMIC_FETCH_DB") or os.path.join(REPO, "database", "fetch.db")


class SourceResult(object):
    """单个源的取数结果。"""

    __slots__ = ('source_id', 'status', 'http', 'archives', 'log', 'country_en')

    def __init__(self, source_id, country_en=''):
        self.source_id = source_id
        self.country_en = country_en
        self.status = 'unchanged'
        self.http = 0
        self.archives = []
        self.log = []

    def as_dict(self):
        return {
            'source_id': self.source_id,
            'status': self.status,
            'http': self.http,
            'archives': len(self.archives),
        }


def make_transport(timeout=FETCH_TIMEOUT):
    """把 probe_channels 的 curl 四层降级包装成内核的传输接口。

    内核的传输接口约定: 返回 (状态码, 原始字节, 响应头字典);
    连接层失败抛 TransportError, 4xx/5xx 抛 HttpStatusError。
    """
    def transport(url, headers=None, timeout_=None):
        extra = None
        if headers:
            extra = ["%s: %s" % (k, v) for k, v in headers.items()]
        code, body, note, resp_headers = probe_channels.http_get_full(
            url, timeout_ or timeout, want_body=True, max_bytes=MAX_BODY_BYTES,
            extra_headers=extra)
        if code == 0:
            raise TransportError("%s: 连接失败(%s)" % (url, note or "未知"))
        return code, body, resp_headers
    return transport


def make_job(transport, url, **opts):
    """按 URL 造一个 URL 作业(条件请求/编码回退/错误忽略都在里面)。"""
    opts.setdefault('user_agent', DEFAULT_USER_AGENT)
    return UrlJob(url=url, transport=transport, **opts)


def make_fetch_fn(conditional=True):
    """构造 fetch_fn(url, state) -> 正文文本。

    作业从 state.job 取(judge_url 会把作业挂到状态上) —— 同一个 URL 的作业是
    一次性的, URL 不同则作业不同。
    """
    def fetch(url, state):
        if not conditional:
            # 逃生开关: 忽略条件请求, 强制重取(防服务器错误 304 导致漏检)
            state.etag = None
            state.last_modified = None
            state.timestamp = None
        job = state.job
        if job is None:
            raise TransportError('%s: 缺少作业实例' % url)
        return job.retrieve(state)

    return fetch


def archive_body(ctx, url, title, body):
    """把正文交给 collect.py 落盘。

    走 collect.py 是为了守住两条硬约束:
      · 配额口径单一来源(collect.count_archives 数 *.meta.json);
      · 落盘格式不变(extract_records.py 直接消费 meta.json + 正文文件)。
    用 --file 传入已抓到的正文, 避免 collect 再抓一遍(那会把条件请求的
    收益吃掉一半)。--kind html 保证扩展名仍是 .html, 与旧取数器一致。

    **必须串行化**: collect.py 的序号(seq = 已有 .meta.json 个数 + 1)与配额
    检查都是"先数后写", 并发调用会算出同一个序号互相覆盖, 或双双越过配额线。
    旧取数器是单线程循环, 没有这个问题; 本内核多源并行, 必须自己加锁。

    **临时文件名必须每次调用唯一**(2026-10-29 修复 P1): 只用标题命名会撞车 ——
    标题来自链接锚文本, 而 fetch_pages 对无锚文本的链接统一填「(无标题)」, 于是
    不同源的空锚链接会拿到同一个文件名; 写入又发生在锁**之外**, 一个线程的重写
    会截断另一个线程正在被 collect.py 读取的正文 → 存下来的是「url 指向 A 页、
    正文却是 B 页」的错配, 直接破坏「source.url + source.quote 必须出自同一原文」
    的溯源硬要求。旧取数器单线程, 没有这个窗口 —— 这是并行化引入的新风险。
    现在文件名带 url 哈希 + 唯一后缀, 每次调用各写各的文件, 天然不串。
    """
    slug = re.sub(r"[^\w一-鿿-]+", "-", title or url or "record").strip("-")[:40] or "record"
    tag = hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:10]
    tmp_path = os.path.join(ctx['tmp_dir'], "%s-%s-%s.html"
                            % (slug, tag, uuid.uuid4().hex[:8]))
    with open(tmp_path, "w", encoding="utf-8", errors="replace") as f:
        f.write(body)
    with ctx['archive_lock']:
        return subprocess.run(
            [sys.executable, os.path.join(REPO, "scripts", "collect.py"),
             "--file", tmp_path, "--url", url, "--title", (title or "")[:80],
             "--kind", "html", "--date", ctx['date'],
             "--max-archives-per-day", str(ctx['max_archives_per_day'])],
            capture_output=True, text=True, timeout=60)


def process_source(ctx, tier, cn, en, sid, entry_url, today, fetch_fn, transport):
    """处理一个源: 抓入口页 → 抽候选 → 逐个判定 → 落盘。"""
    res = SourceResult(sid, en)
    cache = ctx['cache']
    # 该源自己的关注面链(可按源收紧); 没配就用全局默认链
    attention = (ctx['source_options'].get(sid, {}).get('attention_filter')
                  or ctx['attention_filter'])

    # ---- 入口页: 条件请求 + 关注面指纹 ----
    entry = runner.judge_url(cache, entry_url, fetch_fn, attention_filter=attention,
                             job=make_job(transport, entry_url), source_id=sid)
    res.http = entry.http_status or (200 if entry.status in ('new', 'changed') else 0)

    if entry.status in (runner.STATUS_ERROR, runner.STATUS_RETRY):
        res.status = 'error'
        res.log.append("| %s | 不可用 | 入口页抓取失败(%s) |" % (sid, _short(entry.exception)))
        return res
    if entry.status == runner.STATUS_IGNORED:
        res.status = 'unchanged'
        res.log.append("| %s | 跳过 | 入口页错误按源配置忽略 |" % sid)
        return res
    if entry.status == runner.STATUS_UNCHANGED:
        # 入口页未变 → 列表没动 → 没有新条目, 整源跳过(不发 LLM)
        res.status = 'unchanged'
        res.log.append("| %s | 未变 | 入口页关注面指纹未变, 跳过 |" % sid)
        return res

    html = entry.body or ''
    links = fetch_pages.extract_links(html, entry_url)
    confirmed, to_verify = fetch_pages.pick_candidates(links, entry_url, today)

    # ---- 无日期线索的文章型链接: 抓文章页验日期(与旧取数器同口径, 至多验 2 个) ----
    fetched = {}
    for href, anchor in to_verify[:ctx['max_per_source']]:
        art = runner.judge_url(cache, href, fetch_fn, attention_filter=attention,
                               job=make_job(transport, href), source_id=sid)
        if art.status not in (runner.STATUS_NEW, runner.STATUS_CHANGED):
            continue
        d = fetch_pages.page_publish_date(art.body or '')
        if d and fetch_pages.in_window(d, today):
            fetched[href] = (art, anchor, d)

    # ---- 候选池: 列表页带窗内日期的 + 文章页验过的, 按日期倒序 ----
    pool = []
    for href, anchor, d in confirmed:
        pool.append((href, anchor, d))
    for href, (art, anchor, d) in fetched.items():
        pool.append((href, anchor, d))
    pool.sort(key=lambda x: x[2] or today, reverse=True)

    took = 0
    for href, anchor, d in pool:
        if took >= ctx['max_per_source']:
            break
        if ctx['max_total'] and ctx['already'] + ctx['archived'] >= ctx['max_total']:
            res.log.append("| %s | 跳过 | 当日存档已达上限 %d |" % (sid, ctx['max_total']))
            break

        if href in fetched:
            art, anchor, d = fetched[href]
        else:
            art = runner.judge_url(cache, href, fetch_fn, attention_filter=attention,
                                   job=make_job(transport, href), source_id=sid)
        if art.status not in (runner.STATUS_NEW, runner.STATUS_CHANGED):
            # 没变 → 不重复存档(这正是条件请求与指纹判定的收益)
            continue

        p = archive_body(ctx, href, anchor, art.body or '')
        out = (p.stdout or "") + (p.stderr or "")
        if p.returncode != 0:
            res.log.append("| %s | 拒收 | %s |" % (sid, _clean(out)[:60]))
            if "[拒绝]" in out:
                res.log.append("| (全局) | 停止 | 当日存档触顶(collect 拒收) |")
                ctx['capped'] = True
                return res
            continue

        with ctx['archive_lock']:
            ctx['archived'] += 1
        took += 1
        res.archives.append({'url': href, 'title': anchor, 'publish_date': d})
        res.log.append("| %s | 存档 | %s %s |" % (sid, d, href[:70]))
        _append_candidate(ctx, {
            'title': (anchor or "")[:120],
            'url': href,
            'country_en': en,
            'publish_date': d.isoformat() if d else None,
            'source_name': sid,
            'brief': '',
            'topics': [],
        })

    if took == 0:
        res.log.append("| %s | 无窗口内条目 | 链接 %d, 列表页带窗内日期 %d, 文章页验证通过 %d |"
                       % (sid, len(links), len(confirmed), len(fetched)))
    res.status = 'changed' if took else 'unchanged'
    return res


def _clean(s):
    return re.sub(r"\s+", " ", (s or "").replace('"', "」").replace("`", "").replace("$", "")).strip()


def _short(exc):
    return _clean(str(exc))[:60] if exc else "未知"


def _append_candidate(ctx, rec):
    """追加候选清单(多源并行 → 追加要串行, 否则行会交错)。"""
    path = os.path.join(ctx['raw_dir'], "candidates.jsonl")
    with ctx['archive_lock']:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_source_options():
    """逐源的取数选项 {sid: {...}} —— 目前只有 attention_filter(关注面过滤链)。

    刻意做成"生效且可见": 未知过滤器名会告警并回退默认链, 不静默忽略 ——
    配置写了却不生效正是本仓库反复吃过的亏。
    """
    options = {}
    try:
        with open(scan_targets.SOURCES_PATH, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return options

    for sid, meta in scan_targets.parse_sources(text).items():
        chain = scan_targets.parse_attention_filter(meta.get('attention_filter'))
        if not chain:
            continue
        unknown = [k for k in chain if k not in filters.FilterBase.__subclasses__]
        if unknown:
            print("[policy_fetch][WARN] 源 %s 的 attention_filter 含未知过滤器 %s, 回退默认链"
                  % (sid, unknown))
            continue
        options[sid] = {'attention_filter': chain}
    return options


def run(date_str, max_per_source=DEFAULT_MAX_PER_SOURCE, max_total=DEFAULT_MAX_TOTAL,
        max_archives_per_day=0, conditional=True, transport=None, log=print):
    """跑一轮取数。返回 (状态汇总 dict, 逐源日志行列表)。

    transport: 可注入的传输层(测试用假传输); 缺省走 probe_channels 的 curl 四层降级。
    """
    today = datetime.date.fromisoformat(date_str)
    raw_dir = os.path.join(DATA_DIR, "raw", date_str)
    os.makedirs(raw_dir, exist_ok=True)

    picked = scan_targets._pick(date_str)
    source_options = load_source_options()

    # 通道预探: [dead] 直接跳过(与旧取数器同口径, 四层降级在 probe_channels 里)
    alive = {}
    for sid, code, state, _note in probe_channels.probe(date_str):
        alive[sid] = (state == "ok")

    transport = transport or make_transport()
    fetch_fn = make_fetch_fn(conditional=conditional)

    cache = SQLiteCacheStorage(fetch_db_path())
    tmp_dir = tempfile.mkdtemp(prefix="radar-policy-fetch-")
    ctx = {
        'cache': cache,
        'date': date_str,
        'raw_dir': raw_dir,
        'tmp_dir': tmp_dir,
        'attention_filter': DEFAULT_ATTENTION_FILTER,
        'source_options': source_options,
        'max_per_source': max_per_source,
        'max_total': max_total,
        'max_archives_per_day': max_archives_per_day,
        'already': collect_mod.count_archives(raw_dir),
        'archived': 0,
        'capped': False,
        # 多源并行时: 存档计数、候选清单追加、collect.py 落盘都要串行化
        # (计数器的 += 不是原子操作; collect.py 的序号与配额是"先数后写")
        'archive_lock': threading.Lock(),
    }

    log_lines = []
    results = []
    try:
        def _one(item):
            tier, cn, en, sid, url = item
            if not alive.get(sid, False):
                return None
            try:
                return process_source(ctx, tier, cn, en, sid, url, today, fetch_fn, transport)
            except Exception as exc:                    # 单源失败不中断整轮
                res = SourceResult(sid, en)
                res.status = 'error'
                res.log.append("| %s | 不可用 | %s |" % (sid, _short(exc)))
                return res

        for res in runner.run_parallel(_one, picked):
            if res is None:
                continue
            results.append(res)
            log_lines.extend(res.log)

        for _tier, _cn, _en, sid, _url in picked:
            if not alive.get(sid, False):
                log_lines.append("| %s | 不可用 | 预探 dead |" % sid)

        summary = _write_status(ctx, date_str, picked, alive, results)
    finally:
        cache.close()
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return summary, log_lines


def _write_status(ctx, date_str, picked, alive, results):
    """写 fetch-status.json —— 供 run_scan.sh 产物终检消费(补 known-issues H20)。

    口径:
      sources_alive = 总数 - 失败数(预探 dead + 抓取 error 都算失败)
      infra_failure = 存活源为 0 —— 这是「全挂」; 「安静日」是存活源 > 0 但 0 存档。
      两者必须可区分, 否则全挂会伪装成「今日无新增」照常推送(H20)。
    """
    total = len(picked)
    probe_dead = sum(1 for _t, _c, _e, sid, _u in picked if not alive.get(sid, False))
    error_n = sum(1 for r in results if r.status == 'error')
    dead_n = probe_dead + error_n
    alive_n = total - dead_n
    changed = sum(1 for r in results if r.status == 'changed')
    unchanged = sum(1 for r in results if r.status == 'unchanged')
    summary = {
        'date': date_str,
        'sources_total': total,
        'sources_alive': alive_n,
        'sources_dead': dead_n,
        'sources_changed': changed,
        'sources_unchanged': unchanged,
        'archives': ctx['archived'],
        # 全挂(存活源 0)与「安静日」(存活源 > 0 但 0 存档)必须可区分 —— H20
        'infra_failure': alive_n == 0,
        'per_source': [r.as_dict() for r in sorted(results, key=lambda x: x.source_id)],
    }
    path = os.path.join(ctx['raw_dir'], "fetch-status.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return summary


def _selftest():
    """离线自检: 不联网, 用假传输跑通「条件请求 → 指纹 → 四态判定」闭环。"""
    from fetchlib.cache import SQLiteCacheStorage
    tmp = tempfile.mkdtemp(prefix="radar-selftest-")
    try:
        cache = SQLiteCacheStorage(os.path.join(tmp, "fetch.db"))
        calls = []

        def fake_transport(url, headers, timeout):
            """假传输: 带上 If-None-Match 就回 304(条件请求命中)。"""
            calls.append(url)
            if headers and headers.get('If-None-Match'):
                return 304, b'', {'ETag': '"v1"'}
            body = ("<html><body><nav>导航</nav><article><p>政策 A</p></article>"
                    "</body></html>").encode('utf-8')
            return 200, body, {'ETag': '"v1"'}

        fetch_fn = make_fetch_fn(conditional=True)
        r1 = runner.judge_url(cache, "https://example.org/a", fetch_fn,
                              attention_filter=DEFAULT_ATTENTION_FILTER,
                              job=make_job(fake_transport, "https://example.org/a"))
        r2 = runner.judge_url(cache, "https://example.org/a", fetch_fn,
                              attention_filter=DEFAULT_ATTENTION_FILTER,
                              job=make_job(fake_transport, "https://example.org/a"))
        ok = (r1.status == runner.STATUS_NEW and r2.status == runner.STATUS_UNCHANGED)
        print("[selftest] 首轮=%s 次轮=%s(条件请求命中) → %s"
              % (r1.status, r2.status, "通过" if ok else "失败"))
        cache.close()
        return 0 if ok else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="urlwatch 内核取数器(条件请求 + 关注面变更判定)")
    ap.add_argument("--date", default=biz_today(), help="业务日期 YYYY-MM-DD(默认东八区今天)")
    ap.add_argument("--max-per-source", type=int, default=DEFAULT_MAX_PER_SOURCE)
    ap.add_argument("--max-total", type=int, default=DEFAULT_MAX_TOTAL)
    ap.add_argument("--max-archives-per-day", type=int, default=None,
                    help="透传 collect.py 的当日存档条数上限(默认 0=不限)")
    ap.add_argument("--max-files-per-day", type=int, default=None,
                    help="[旧名, 已更名] 同 --max-archives-per-day")
    ap.add_argument("--no-conditional", action="store_true",
                    help="忽略条件请求, 强制全量重取(防服务器错误 304 导致漏检)")
    ap.add_argument("--status-only", action="store_true",
                    help="只读当日 fetch-status.json: 判基础设施故障(存活源 0)时退出码 3, "
                         "否则 0; 读不到则 2(run_scan.sh 终检用)")
    ap.add_argument("--selftest", action="store_true", help="离线自检(不联网)")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    if args.status_only:
        path = os.path.join(DATA_DIR, "raw", args.date, "fetch-status.json")
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            print("[policy_fetch] 读状态失败(%s): %s" % (path, exc))
            return 2
        if data.get('infra_failure'):
            print("[policy_fetch] 取数全挂(存活源 0) —— 基础设施故障, 不是安静日")
            return 3
        print("[policy_fetch] 取数状态正常(存活源 %s / 总数 %s)"
              % (data.get('sources_alive'), data.get('sources_total')))
        return 0

    cap = args.max_archives_per_day
    if args.max_files_per_day is not None:
        if cap is not None and cap != args.max_files_per_day:
            print("[错误] --max-archives-per-day(%s) 与 --max-files-per-day(%s) 不一致, 已中止。"
                  % (cap, args.max_files_per_day))
            return 2
        cap = args.max_files_per_day
    if cap is None:
        cap = collect_mod.DEFAULT_MAX_ARCHIVES_PER_DAY

    try:
        summary, log_lines = run(args.date, args.max_per_source, args.max_total, cap,
                                 conditional=not args.no_conditional)
    except (OSError, ValueError) as exc:
        print("[policy_fetch][错误] %s" % exc)
        return 2

    raw_dir = os.path.join(DATA_DIR, "raw", args.date)
    with open(os.path.join(raw_dir, "search-log.md"), "a", encoding="utf-8") as f:
        f.write("\n## 确定性取数(urlwatch 内核) %s\n\n" % datetime.datetime.now().strftime("%F %T"))
        for line in log_lines:
            f.write(line + "\n")

    print("[policy_fetch] %s: 新存档 %d 条; 源 %d(存活 %d/变更 %d/未变 %d/失败 %d)%s"
          % (args.date, summary['archives'], summary['sources_total'],
             summary['sources_alive'], summary['sources_changed'],
             summary['sources_unchanged'], summary['sources_dead'],
             ", 触顶停止" if summary.get('capped') else ""))
    if summary['infra_failure']:
        print("[policy_fetch][严重] 存活源 0 —— 判基础设施故障(不是安静日), 不得推送「今日无新增」。")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
