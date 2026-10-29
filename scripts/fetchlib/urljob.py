# -*- coding: utf-8 -*-
"""URL 作业 —— 移植自 urlwatch 2.29(thp/urlwatch master)。

来源: urlwatch-master/lib/urlwatch/jobs.py:283-428(UrlJob)

移植了哪几块(与本项目收益直接相关的):
  · 条件请求(jobs.py:311-321): If-None-Match / If-Modified-Since; 重试时撤销条件头
    —— 上一轮 ETag 命中 304 即判未变, 不再重复下载与比对。
  · 304 处理 + ETag 回写(jobs.py:369-373): 命中条件请求抛 NotModifiedError,
    并把新的 ETag/Last-Modified 记回作业状态(下一轮用)。
  · 编码回退链(jobs.py:375-398): utf-8 → latin1 → utf-8(ignore) → ascii(ignore)。
  · 自定义头合并(jobs.py:400-408): 大小写不敏感覆盖。
  · 错误格式化与可忽略判定(jobs.py:410-428): HTTP 错误压成一行; 连接类错误可按源配置忽略。

有意不移植:
  · requests 依赖 → 传输层可注入(默认 urllib, 生产注入 probe_channels 的 curl
    四层降级; 见 policy_fetch.py)。理由: probe_channels 的降级链(SSL 链错误 /
    403 去 UA / 慢站加长)是 10-25 死源分诊的产物, 生产在用的源靠它复活,
    裸 urllib 做不到。
  · proxies / cookies / POST body —— 本项目的源全是公开页 GET, 用不上。
  · shell 执行面(ShellJob) —— 见 jobbase.py 说明。
"""
import email.utils
import logging
import re
import urllib.error
import urllib.request

from .jobbase import Job, NotModifiedError

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; policy-monitor/1.0; +official-sources-check)"
DEFAULT_TIMEOUT = 30


class TransportError(Exception):
    """连接层失败(DNS/连接/超时/SSL)—— 与 HTTP 错误区分开。"""
    ...


class HttpStatusError(Exception):
    """HTTP 错误状态(4xx/5xx)。"""

    def __init__(self, status, message='', url=''):
        super().__init__('%s %s %s' % (status, message, url))
        self.status = status
        self.message = message
        self.url = url


def urllib_get(url, headers=None, timeout=DEFAULT_TIMEOUT):
    """默认传输层: stdlib urllib。返回 (状态码, 原始字节, 响应头字典)。"""
    req = urllib.request.Request(url, headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return resp.getcode(), body, dict(resp.headers)
    except urllib.error.HTTPError as exc:
        # urllib 对 3xx/4xx/5xx 一律抛 HTTPError(304 也在内)
        try:
            body = exc.read()
        except Exception:
            body = b''
        if exc.code == 304:
            return 304, body, dict(exc.headers or {})
        raise HttpStatusError(exc.code, getattr(exc, 'reason', '') or '', url)
    except urllib.error.URLError as exc:
        raise TransportError('%s: %s' % (url, exc.reason))
    except OSError as exc:
        raise TransportError('%s: %s' % (url, exc))


class UrlJob(Job):
    """抓一个 URL(带条件请求)。"""

    __kind__ = 'url'

    __required__ = ('url',)
    __optional__ = ('headers', 'timeout', 'encoding', 'user_agent',
                    'ignore_connection_errors', 'ignore_http_error_codes',
                    'ignore_timeout_errors', 'ignore_too_many_redirects',
                    'ignore_incomplete_reads')

    CHARSET_RE = re.compile(r'text/(html|plain); charset=([^;]*)')

    def __init__(self, transport=None, **kwargs):
        super().__init__(**kwargs)
        # 传输层可注入: 默认 urllib; 生产注入 curl 四层降级(见 policy_fetch.py)
        self.transport = transport or urllib_get

    def get_location(self):
        return self.user_visible_url or self.url

    def set_base_location(self, location):
        self.url = location

    def build_headers(self, job_state):
        """条件请求头 + 自定义头。"""
        headers = {'User-agent': self.user_agent or DEFAULT_USER_AGENT}

        if job_state.etag:
            headers['If-None-Match'] = job_state.etag
        if job_state.last_modified:
            headers['If-Modified-Since'] = job_state.last_modified
        elif job_state.timestamp:
            headers['If-Modified-Since'] = email.utils.formatdate(job_state.timestamp)

        if job_state.tries > 0:
            # 重试时必须强制取新内容: 撤销条件头, 否则重试只会再拿到一次 304
            headers.pop('If-None-Match', None)
            headers.pop('If-Modified-Since', None)
            headers['Cache-Control'] = 'max-age=172800'
            headers['Expires'] = email.utils.formatdate()

        if self.headers:
            self.add_custom_headers(headers)

        return headers

    def add_custom_headers(self, headers):
        """合入自定义头: 大小写不敏感, 自定义值覆盖预填值。"""
        headers_to_remove = [x for x in headers if x.lower() in [y.lower() for y in self.headers]]
        for header in headers_to_remove:
            headers.pop(header, None)
        headers.update(self.headers)

    def retrieve(self, job_state):
        headers = self.build_headers(job_state)
        timeout = self.timeout if self.timeout else DEFAULT_TIMEOUT

        status, raw, resp_headers = self.transport(self.url, headers, timeout)

        if status == 304:
            raise NotModifiedError()

        if status < 200 or status >= 400:
            raise HttpStatusError(status, '', self.url)

        # 条件请求的凭据回写 —— 下一轮用
        job_state.etag = resp_headers.get('ETag') or resp_headers.get('etag')
        job_state.last_modified = resp_headers.get('Last-Modified') or resp_headers.get('last-modified')

        return self.decode(raw, resp_headers)

    def decode(self, raw, resp_headers):
        """编码回退链(移植自 jobs.py:375-398)。"""
        content_type = resp_headers.get('Content-Type', '') or resp_headers.get('content-type', '')
        content_type_match = self.CHARSET_RE.match(content_type)

        if not content_type_match and not self.encoding:
            try:
                try:
                    return raw.decode('utf-8')
                except UnicodeDecodeError:
                    return raw.decode('latin1')
            except LookupError:
                return raw.decode('ascii', 'ignore')

        if self.encoding:
            try:
                return raw.decode(self.encoding, 'replace')
            except LookupError:
                return raw.decode('utf-8', 'replace')

        if content_type_match:
            charset = content_type_match.group(2).strip()
            try:
                return raw.decode(charset, 'replace')
            except LookupError:
                pass

        return raw.decode('utf-8', 'replace')

    def format_error(self, exception, tb):
        if isinstance(exception, (HttpStatusError, TransportError)):
            # HTTP 错误不需要整段 traceback
            return str(exception)
        return super().format_error(exception, tb)

    def should_ignore_error(self, exception) -> bool:
        if isinstance(exception, TransportError):
            # 连接层错误(超时/连接失败/SSL/重定向过多): 源配置任一 ignore_* 打开即可忽略。
            # 与上游的差别: 上游按 requests 异常类型逐个区分, 这里的传输层把连接类
            # 失败统一收敛成 TransportError, 故几个开关等价合并。
            return bool(self.ignore_connection_errors
                        or self.ignore_timeout_errors
                        or self.ignore_too_many_redirects
                        or self.ignore_incomplete_reads)
        if isinstance(exception, HttpStatusError):
            return should_ignore_http_code(exception.status, self.ignore_http_error_codes)
        return False


def should_ignore_http_code(status_code, codes_to_ignore) -> bool:
    """该 HTTP 状态码是否应被忽略(移植自 urlwatch util.py:143-159)。

    codes_to_ignore 可为单个码、列表, 或逗号分隔字符串; 支持 '4xx' 这类码类。
    """
    ignored_codes = []
    if isinstance(codes_to_ignore, int) and codes_to_ignore == status_code:
        return True
    elif isinstance(codes_to_ignore, str):
        ignored_codes = [s.strip().lower() for s in codes_to_ignore.split(',')]
    elif isinstance(codes_to_ignore, list):
        ignored_codes = [str(s).strip().lower() for s in codes_to_ignore]
    return str(status_code) in ignored_codes or '%dxx' % (status_code // 100) in ignored_codes
