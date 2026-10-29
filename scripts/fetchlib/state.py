# -*- coding: utf-8 -*-
"""作业状态机 —— 移植自 urlwatch 2.29(thp/urlwatch master)。

来源: urlwatch-master/lib/urlwatch/handler.py:49-174(JobState)

移植了哪几块:
  · 状态载入/回写(load / save)—— 换成本项目的 URL 粒度缓存(fetch_state +
    page_snapshot), 条件请求凭据(etag / last_modified)随状态走。
  · diff 生成(_generate_diff)—— difflib.unified_diff + email.utils 时间戳;
    **不移植** diff_tool(外部 diff 命令, 执行面红线)。
  · 历史近似基线(get_close_matches)—— 页面回退到旧状态时, 用最接近的历史版本
    做 diff 基线, 避免拿两代前的快照比出满屏噪音。

上游把「取数 → 过滤 → 比对」都塞在 JobState.process() 里; 本移植把它拆成
「取数」(urljob) 与「判定」(runner) 两段, 因为本项目的取数单元是 URL 而作业
单元是源, 一个源会展开出多个 URL, 拆开才好并行与复用。
"""
import difflib
import email.utils
import logging
import time
import traceback

logger = logging.getLogger(__name__)


class JobState(object):
    """单个 URL 的取数状态。"""

    def __init__(self, cache_storage, url, job=None, source_id=None):
        self.cache_storage = cache_storage
        self.url = url
        self.job = job
        self.source_id = source_id

        self.old_data = None          # 上一版正文快照
        self.old_sha1 = None          # 上一版关注面指纹
        self.new_data = None          # 本次正文
        self.new_sha1 = None          # 本次关注面指纹
        self.history_data = {}        # {正文: 时间戳} 历史版本
        self.timestamp = None          # 上次抓取时间(epoch)
        self.current_timestamp = None
        self.exception = None
        self.traceback = None
        self.tries = 0
        self.etag = None
        self.last_modified = None
        self.error_ignored = False
        self._generated_diff = None

    def load(self):
        """从缓存载入上一版状态(正文 / 指纹 / 条件请求凭据 / 重试计数)。"""
        st = self.cache_storage.load_state(self.url) or {}
        self.etag = st.get('etag')
        self.last_modified = st.get('last_modified')
        self.tries = st.get('tries') or 0
        self.timestamp = st.get('last_fetched')

        body, sha1, ts = self.cache_storage.load_body(self.url)
        if body is not None:
            self.old_data = body
        if sha1:
            self.old_sha1 = sha1
        if ts:
            self.timestamp = ts

        count = 0
        if self.job is not None:
            try:
                count = int(getattr(self.job, 'compared_versions', 0) or 0)
            except (TypeError, ValueError):
                count = 0
        if count > 1:
            self.history_data = {b: t for b, t in self.cache_storage.history(self.url, count)}

    def save(self, content_sha1=None, status=None, archive=False, timestamp=None):
        """回写状态(条件请求凭据 / 指纹 / 时间戳 / 重试计数)。"""
        if self.new_data is None and self.exception is not None:
            # 抓取失败: 保留上一版正文当"当前版", 避免下一轮误判成新增
            self.new_data = self.old_data

        ts = timestamp if timestamp is not None else time.time()
        self.current_timestamp = ts

        self.cache_storage.save_state(
            self.url,
            source_id=self.source_id,
            etag=self.etag,
            last_modified=self.last_modified,
            content_sha1=content_sha1 if content_sha1 is not None else self.old_sha1,
            last_status=status,
            last_fetched=ts,
            last_archived=1 if archive else None,
            tries=self.tries,
        )

    def record_error(self, exception, tb=None):
        self.exception = exception
        self.traceback = tb if tb is not None else traceback.format_exc()
        self.tries += 1

    def get_diff(self):
        """本次变化的有界证据(unified diff)。"""
        if self._generated_diff is None:
            self._generated_diff = self._generate_diff()
        return self._generated_diff

    def _generate_diff(self):
        old = self.old_data if self.old_data is not None else ''
        new = self.new_data if self.new_data is not None else ''

        # 历史近似基线: 页面回退到旧状态时, 拿最接近的历史版本比, 避免满屏噪音
        if self.history_data:
            close_matches = difflib.get_close_matches(new, self.history_data, n=1)
            if close_matches:
                old = close_matches[0]
                self.timestamp = self.history_data[close_matches[0]]

        timestamp_old = email.utils.formatdate(self.timestamp, localtime=True)
        timestamp_new = email.utils.formatdate(self.current_timestamp or time.time(), localtime=True)
        return '\n'.join(difflib.unified_diff(old.splitlines(),
                                               new.splitlines(),
                                               '@', '@', timestamp_old, timestamp_new, lineterm=''))
