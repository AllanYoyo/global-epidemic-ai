# -*- coding: utf-8 -*-
"""变更判定与并发执行 —— 移植自 urlwatch 2.29(thp/urlwatch master)。

来源: urlwatch-master/lib/urlwatch/worker.py:41-113(并发 + 四态判定)

移植了哪几块:
  · 四态判定(worker.py:93-109): new / changed / unchanged / error。
  · 重试计数语义(worker.py:69-92): tries 未达 max_tries → retry(等下一轮),
    达到 → error(上报)。
  · 并发执行(worker.py:44-50): ThreadPoolExecutor, 默认 10 并发。

**一处刻意偏离**: 上游 run_parallel 遇到任一异常就 raise, 整轮崩掉。
本项目的既有语义是「单源失败只影响单源, 逐源留痕 search-log」(见 fetch_pages.py
与 known-issues.md H3/H20), 所以这里改成**异常隔离**: 单项异常不中断整轮,
由调用方汇总状态。H3/H20 的教训正是「取数失败被静默吞掉 / 全挂与安静日不可区分」
—— 隔离之后必须把状态汇总出来(见 fetch-status.json), 否则就是换了个地方静默。

判定用的是「关注面指纹」而不是整页文本: 政府站点的导航栏/日期戳/页脚每轮都在变,
整页比对会把 47 个源全判成 changed, 涌进 LLM 抽取 → 重演 2026-10-24 token 爆量。
"""
import concurrent.futures
import hashlib
import logging

from .filters import apply_chain
from .jobbase import NotModifiedError
from .state import JobState

logger = logging.getLogger(__name__)

MAX_WORKERS = 10

STATUS_NEW = 'new'
STATUS_CHANGED = 'changed'
STATUS_UNCHANGED = 'unchanged'
STATUS_ERROR = 'error'
STATUS_RETRY = 'retry'
STATUS_IGNORED = 'ignored'

DEFAULT_ATTENTION_FILTER = ['html2text', 'strip', 'sha1sum']
DEFAULT_RETAIN_LIMIT = 3


class _PendingSave(object):
    """new/changed 判定的**延迟落库**句柄(judge_url defer_save=True 时挂到结果上)。

    为什么存在: 入口页快照若在候选处理之前落库, 候选处理半途失败(存档超时 /
    collect 崩溃 / 进程被杀)后, 下一轮入口页 304/未变 → 整源被跳过, 这批候选
    直到列表页再变才会被补抓 —— 瞬时失败被放大成永久丢候选。延迟提交后, 失败
    即不提交, 下一轮按**旧**凭据(etag/指纹)发条件请求会拿回 200 全文, 本源重新
    判定, 候选自动重驱(已存档的候选由 collect 按日去重, 不会重复落盘)。
    """

    __slots__ = ('_state', '_cache', '_url', '_sha1', '_body', '_retain_limit', '_done')

    def __init__(self, state, cache, url, sha1, body, retain_limit):
        self._state = state
        self._cache = cache
        self._url = url
        self._sha1 = sha1
        self._body = body
        self._retain_limit = retain_limit
        self._done = False

    def commit(self):
        if self._done:
            return
        # etag / last_modified 在 fetch 时已写进 state(内存), 这里随状态一并落库;
        # 不提交时它们也不落库 —— 下一轮才会用旧凭据重新取全文。
        self._state.save(content_sha1=self._sha1, status=200)
        self._cache.save_body(self._url, self._sha1, self._body,
                              self._state.current_timestamp)
        try:
            self._cache.clean(self._url, self._retain_limit)
        except Exception:
            logger.debug('快照裁剪失败(不影响判定): %s', self._url, exc_info=True)
        self._done = True


class JudgeResult(object):
    """单个 URL 的判定结果。"""

    __slots__ = ('url', 'source_id', 'status', 'body', 'fingerprint', 'diff',
                 'exception', 'traceback', 'http_status', 'title', 'publish_date',
                 'pending_save')

    def __init__(self, url, source_id=None):
        self.url = url
        self.source_id = source_id
        self.status = STATUS_UNCHANGED
        self.body = None
        self.fingerprint = None
        self.diff = None
        self.exception = None
        self.traceback = None
        self.http_status = None
        self.title = None
        self.publish_date = None
        self.pending_save = None

    @property
    def changed(self):
        return self.status in (STATUS_NEW, STATUS_CHANGED)

    def commit_pending(self):
        """提交延迟落库(未延迟或已提交时空操作)。见 judge_url 的 defer_save。"""
        if self.pending_save is not None:
            self.pending_save.commit()

    def __repr__(self):
        return '<JudgeResult %s %s>' % (self.status, self.url)


def run_parallel(func, items, workers=MAX_WORKERS):
    """并发执行, **单项异常隔离**(刻意偏离 urlwatch worker.py:44-50 的 raise 语义)。"""
    results = []
    items = list(items)
    if not items:
        return results
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(items))) as ex:
        futures = {ex.submit(func, item): item for item in items}
        for fut in concurrent.futures.as_completed(futures):
            item = futures[fut]
            try:
                results.append(fut.result())
            except Exception as exc:
                logger.exception('并发任务异常(已隔离): %r', item)
                results.append(exc)
    return results


def fingerprint(data, attention_filter=None, job=None):
    """关注面指纹 —— 判定「有没有变」的唯一依据。

    未配置过滤器链时, 取正文自身的 sha1; 配置时取链末结果(默认链末是 sha1sum)。
    """
    if data is None:
        return None
    if attention_filter:
        value = apply_chain(attention_filter, data, job=job)
        return value if isinstance(value, str) else str(value)
    if isinstance(data, str):
        data = data.encode('utf-8')
    return hashlib.sha1(data).hexdigest()


def judge_url(cache, url, fetch_fn, attention_filter=None, job=None, source_id=None,
              max_tries=0, retain_limit=DEFAULT_RETAIN_LIMIT, defer_save=False):
    """判定单个 URL 的状态。

    fetch_fn(url, state) -> 正文文本; 命中条件请求时抛 NotModifiedError。
    返回 JudgeResult(状态/正文/指纹/diff)。

    defer_save=True 时, new/changed 的快照**不立即落库**, 而是挂一个待提交句柄
    (result.pending_save), 由调用方在后续处理成功后调 result.commit_pending();
    调用方失败(异常/不提交)则本轮快照整体作废, 下一轮重新判定 —— 见 _PendingSave。
    unchanged / 304 / error 路径不受影响, 照旧立即落库。
    """
    state = JobState(cache, url, job=job, source_id=source_id)
    state.load()

    result = JudgeResult(url, source_id=source_id)

    try:
        body = fetch_fn(url, state)
    except NotModifiedError:
        # 条件请求命中: 资源未改, 无需比对
        state.tries = 0
        state.save(status=304)
        result.status = STATUS_UNCHANGED
        result.http_status = 304
        return result
    except Exception as exc:
        state.record_error(exc)
        ignored = False
        if job is not None:
            try:
                ignored = bool(job.should_ignore_error(exc))
            except Exception:
                ignored = False
        state.error_ignored = ignored
        result.exception = exc
        result.traceback = state.traceback
        if ignored:
            result.status = STATUS_IGNORED
        elif state.tries < max_tries:
            result.status = STATUS_RETRY
        else:
            result.status = STATUS_ERROR
        state.save(status=None)
        return result

    state.new_data = body
    fp = fingerprint(body, attention_filter, job=job)
    state.new_sha1 = fp
    result.body = body
    result.fingerprint = fp

    if state.old_data is None and state.old_sha1 is None:
        status = STATUS_NEW
    elif state.old_sha1 == fp:
        status = STATUS_UNCHANGED
    else:
        status = STATUS_CHANGED
    result.status = status

    state.tries = 0
    if status in (STATUS_NEW, STATUS_CHANGED):
        if defer_save:
            result.pending_save = _PendingSave(state, cache, url, fp, body, retain_limit)
        else:
            state.save(content_sha1=fp, status=200)
            cache.save_body(url, fp, body, state.current_timestamp)
            try:
                cache.clean(url, retain_limit)
            except Exception:
                logger.debug('快照裁剪失败(不影响判定): %s', url, exc_info=True)
        result.diff = state.get_diff()
    else:
        state.save(content_sha1=fp, status=200)

    return result
