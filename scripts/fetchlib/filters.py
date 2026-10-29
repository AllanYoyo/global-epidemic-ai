# -*- coding: utf-8 -*-
"""过滤器链 —— 移植自 urlwatch 2.29(thp/urlwatch master)。

来源: urlwatch-master/lib/urlwatch/filters.py:93-210(FilterBase)、
      :309-331(Html2TextFilter)、:445-502(Grep/ InverseGrep / Strip)、
      :635-646(Sha1Filter)

与上游的差异:
  1. **不移植** css / xpath 过滤器(上游靠 lxml + cssselect, 会破坏「不加硬依赖」
     红线)。关注面压缩改用 stdlib html.parser + 正则实现, 能力等价于上游
     「取某个容器内的文本」这一档, 够用且零依赖。
  2. html2text 上游走 .html2txt(内部依赖 bs4 或自研解析); 本移植用
     stdlib html.parser 重写, 并新增 container 子参数 —— 直接服务于本项目的
     「关注面」判定(见 runner.fingerprint)。

「关注面」是这个模块在本项目里的主要用途:
  政府站点的导航栏/日期戳/页脚每轮都在变, 整页比对会把 47 个源全判成 changed,
  涌进 LLM 抽取 → 重演 2026-10-24 token 爆量事故。所以先跑
  「html2text → strip → sha1sum」把页面压成关注面指纹, 指纹不变即跳过 LLM。
"""
import hashlib
import itertools
import logging
import re
from html.parser import HTMLParser

from .metaclass import TrackSubClasses

logger = logging.getLogger(__name__)


class FilterBase(object, metaclass=TrackSubClasses):
    __subclasses__ = {}
    __anonymous_subclasses__ = []

    def __init__(self, job, state):
        self.job = job
        self.state = state

    @classmethod
    def filter_documentation(cls):
        result = []
        for sc in TrackSubClasses.sorted_by_kind(cls):
            default_subfilter = getattr(sc, '__default_subfilter__', None)
            result.append('  * %s - %s' % (sc.__kind__, sc.__doc__))
            if hasattr(sc, '__supported_subfilters__'):
                for key, doc in sc.__supported_subfilters__.items():
                    result.append('      %s%s%s ... %s' % ('[' if key == default_subfilter else '', key,
                                                           ']' if key == default_subfilter else '', doc))
        result.append('\n[] ... 子参数可写成无名值\n')
        return '\n'.join(result)

    @classmethod
    def auto_process(cls, state, data):
        """自动匹配的过滤器(MATCH 命中就自动应用)。本移植未启用自动匹配, 保留接口。"""
        filters = itertools.chain((filtercls for _, filtercls in
                                   sorted(cls.__subclasses__.items(), key=lambda k_v: k_v[0])),
                                  cls.__anonymous_subclasses__)

        for filtercls in filters:
            filter_instance = filtercls(state.job, state)
            if filter_instance.match():
                logger.info('自动应用过滤器 %r', filter_instance)
                data = filter_instance.filter(data, None)

        return data

    @classmethod
    def normalize_filter_list(cls, filter_spec):
        """把过滤器规格归一成 (kind, subfilter) 序列。"""
        for filter_kind, subfilter in cls._internal_normalize_filter_list(filter_spec):
            filtercls = cls.__subclasses__.get(filter_kind, None)

            if filtercls is None:
                raise ValueError('未知过滤器类型: %s (子参数 %s)' % (filter_kind, subfilter))

            if getattr(filtercls, '__no_subfilter__', False) and subfilter:
                raise ValueError('%s 不支持子参数' % filter_kind)

            if isinstance(subfilter, dict) and hasattr(filtercls, '__supported_subfilters__'):
                provided_keys = set(subfilter.keys())
                allowed_keys = set(filtercls.__supported_subfilters__.keys())
                unknown_keys = provided_keys.difference(allowed_keys)
                if unknown_keys and '<any>' not in allowed_keys:
                    raise ValueError('过滤器 "%s" 不支持子参数 %s (支持: %s)'
                                     % (filter_kind, unknown_keys, allowed_keys))

            yield filter_kind, subfilter

    @classmethod
    def _internal_normalize_filter_list(cls, filter_spec):
        if isinstance(filter_spec, str):
            # 上游遗留的字符串写法 "filter1:param1,filter2" —— 已废弃, 这里直接报错
            raise ValueError('字符串式过滤器规格已废弃, 请改用列表: %r' % (filter_spec,))

        if isinstance(filter_spec, list):
            for item in filter_spec:
                if isinstance(item, str):
                    filter_kind, subfilter = item, None
                elif isinstance(item, dict):
                    filter_kind, subfilter = next(iter(item.items()))
                else:
                    continue

                filtercls = cls.__subclasses__.get(filter_kind, None)

                if isinstance(subfilter, dict):
                    yield filter_kind, subfilter
                elif subfilter is None:
                    yield filter_kind, {}
                elif hasattr(filtercls, '__default_subfilter__'):
                    yield filter_kind, {getattr(filtercls, '__default_subfilter__'): subfilter}
                else:
                    yield filter_kind, subfilter

    @classmethod
    def process(cls, filter_kind, subfilter, state, data):
        logger.info('应用过滤器 %r, 子参数 %r', filter_kind, subfilter)
        filtercls = cls.__subclasses__.get(filter_kind, None)
        if filtercls is None:
            raise ValueError('未知过滤器类型: %s' % (filter_kind,))
        return filtercls(state.job, state).filter(data, subfilter)

    @classmethod
    def filter_chain_needs_bytes(cls, filter):
        """本移植不含字节类过滤器(上游的 pdf2text/ocr 需要), 恒为 False。"""
        return False

    def match(self):
        return False

    def filter(self, data, subfilter):
        raise NotImplementedError()


# ---------------------------------------------------------------- 关注面提取

# 结构性块级标签: 这些标签的边界要断行, 否则整页正文会挤成一行
BLOCK_TAGS = frozenset((
    'address', 'article', 'aside', 'blockquote', 'br', 'dd', 'div', 'dl', 'dt',
    'fieldset', 'figcaption', 'figure', 'footer', 'form', 'h1', 'h2', 'h3', 'h4',
    'h5', 'h6', 'header', 'hr', 'li', 'main', 'nav', 'ol', 'p', 'pre', 'section',
    'table', 'tbody', 'td', 'tfoot', 'th', 'thead', 'tr', 'ul',
))

# 噪声容器: 导航/页脚/侧栏里的东西每次都在变, 不属于「关注面」
NOISE_TAGS = frozenset(('script', 'style', 'noscript', 'template', 'nav',
                         'header', 'footer', 'aside', 'form', 'svg'))

# 默认优先关注的容器标签(页面里有就取容器里的, 没有就取全页去噪后的正文)
DEFAULT_CONTAINERS = ('article', 'main')


class _TextExtractor(HTMLParser):
    """把 HTML 抽成纯文本行。

    stdlib html.parser 实现, 不依赖 bs4/lxml。三种关注面:
      · 容器优先: 命中 container 标签则只取容器内的文本;
      · 噪声剔除: nav/header/footer/aside/script/style 一律不进正文;
      · 块级断行: 块级标签边界断行, 便于后续按行比对。
    """

    def __init__(self, containers=()):
        super().__init__(convert_charrefs=True)
        self.containers = tuple(containers or ())
        self.parts = []
        self.depth = 0
        self.noise_depth = 0
        self.container_depth = 0
        self.container_hit = False

    def handle_starttag(self, tag, attrs):
        self.depth += 1
        if self.container_depth == 0 and tag in self.containers:
            self.container_depth = self.depth
            self.container_hit = True
        if tag in NOISE_TAGS:
            self.noise_depth += 1
        if tag in BLOCK_TAGS:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in NOISE_TAGS and self.noise_depth:
            self.noise_depth -= 1
        if tag in BLOCK_TAGS:
            self.parts.append('\n')
        if self.container_depth == self.depth and tag in self.containers:
            self.container_depth = 0
        self.depth = max(0, self.depth - 1)

    def handle_startendtag(self, tag, attrs):
        # <br/> 这类自闭合标签: 只断行, 不进/出容器
        if tag in BLOCK_TAGS:
            self.parts.append('\n')

    def handle_data(self, data):
        if self.noise_depth:
            return
        if self.container_depth and self.depth < self.container_depth:
            # 容器之外的内容不要
            return
        self.parts.append(data)

    def text(self):
        raw = ''.join(self.parts)
        lines = []
        for line in raw.split('\n'):
            line = re.sub(r'[ \t\r\f\v]+', ' ', line).strip()
            if line:
                lines.append(line)
        return '\n'.join(lines)


def html_to_text(html, containers=()):
    """HTML → 纯文本行。容器命中则只取容器内文本, 否则取去噪后的全页正文。"""
    if not html:
        return ''
    try:
        parser = _TextExtractor(containers)
        parser.feed(html)
        parser.close()
        text = parser.text()
    except Exception:  # 畸形 HTML 不该让整轮取数炸掉
        logger.debug('HTML 解析失败, 退化为正则去标签', exc_info=True)
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'[ \t]+', ' ', text)
    return text.strip()


class Html2TextFilter(FilterBase):
    """把 HTML 转成纯文本(可限定容器, 用于关注面提取)"""

    __kind__ = 'html2text'

    __supported_subfilters__ = {
        'container': '关注的容器标签(逗号分隔, 如 article,main); 缺省=自动探测, '
                     '页面没有这些容器时取去噪后的全页正文',
    }

    def filter(self, data, subfilter):
        if not isinstance(data, str):
            raise ValueError('html2text 过滤器需要文本输入')
        raw_container = (subfilter or {}).get('container')
        if raw_container:
            containers = tuple(c.strip().lower() for c in str(raw_container).split(',') if c.strip())
        else:
            containers = DEFAULT_CONTAINERS
        return html_to_text(data, containers)


class GrepFilter(FilterBase):
    """只保留匹配正则的行"""

    __kind__ = 'grep'

    __supported_subfilters__ = {
        're': '保留匹配该正则的行(必填)',
    }

    def filter(self, data, subfilter):
        if 're' not in (subfilter or {}):
            raise ValueError('%s 过滤器需要 re 子参数' % self.__kind__)
        return '\n'.join(line for line in data.splitlines()
                         if re.search(subfilter['re'], line) is not None)


class InverseGrepFilter(FilterBase):
    """剔除匹配正则的行"""

    __kind__ = 'inverse-grep'

    __supported_subfilters__ = {
        're': '剔除匹配该正则的行(必填)',
    }

    def filter(self, data, subfilter):
        if 're' not in (subfilter or {}):
            raise ValueError('%s 过滤器需要 re 子参数' % self.__kind__)
        return '\n'.join(line for line in data.splitlines()
                         if re.search(subfilter['re'], line) is None)


class StripFilter(FilterBase):
    """去掉首尾空白"""

    __kind__ = 'strip'

    __no_subfilter__ = True

    def filter(self, data, subfilter):
        return data.strip()


class Sha1Filter(FilterBase):
    """计算内容的 SHA-1 校验和(关注面指纹)"""

    __kind__ = 'sha1sum'

    __no_subfilter__ = True

    def filter(self, data, subfilter):
        if isinstance(data, str):
            data = data.encode('utf-8')
        return hashlib.sha1(data).hexdigest()


def apply_chain(chain, data, job=None, state=None):
    """按规格跑一串过滤器, 返回末结果。

    chain: 过滤器规格(列表), 如 ['html2text', 'strip', 'sha1sum']。
    空规格返回原值 —— 调用方据此判定「未配置关注面」。
    """
    if not chain:
        return data
    for filter_kind, subfilter in FilterBase.normalize_filter_list(chain):
        data = FilterBase.process(filter_kind, subfilter, _State(job, state), data)
    return data


class _State(object):
    """极简状态壳 —— FilterBase.process 只用到 state.job。"""

    __slots__ = ('job', 'raw')

    def __init__(self, job, raw=None):
        self.job = job
        self.raw = raw
