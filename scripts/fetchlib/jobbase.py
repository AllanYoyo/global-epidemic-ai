# -*- coding: utf-8 -*-
"""作业模型 —— 移植自 urlwatch 2.29(thp/urlwatch master)。

来源: urlwatch-master/lib/urlwatch/jobs.py:88-235(JobBase / Job)

与上游的差异(有意为之, 均为安全与领域适配, 非功能缺失):
  1. 裁掉 ShellJob(jobs.py:237-280, shell=True) —— 不引入任意命令执行面。
     known-issues.md H6 正在治理注入面, 扩大命令执行面与治理方向相悖。
  2. Job.__optional__ 去掉 'diff_tool' —— 外部 diff 命令同样是执行面,
     diff 一律用 stdlib difflib。
  3. JobBase.unserialize 的 kind 自动推断保留, 但 policy_fetch 侧直接构造作业,
     不依赖自动推断(自动推断在新增作业类型时容易撞键集)。

作业模型在本项目里的角色:
  外层作业 = 一个情报源(入口 URL);  内层作业 = 一个具体页面 URL(条件请求的
  最小粒度)。两者的分工见 policy_fetch.py。
"""
import hashlib
import logging
import re
from typing import Iterable, Optional, FrozenSet

from .metaclass import TrackSubClasses

logger = logging.getLogger(__name__)


class NotModifiedError(Exception):
    """HTTP 304(资源未修改)—— 条件请求命中, 本轮无需再比内容。"""
    ...


class JobBase(object, metaclass=TrackSubClasses):
    __subclasses__ = {}

    __required__ = ()
    __optional__ = ()

    def __init__(self, **kwargs):
        # 可选键缺省置 None
        for k in self.__optional__:
            if k not in kwargs:
                setattr(self, k, None)

        # 必填键缺失直接报错(配置写错要在最早就炸)
        for k in self.__required__:
            if k not in kwargs:
                raise ValueError('缺少必填字段 %s: %r' % (k, kwargs))

        for k, v in list(kwargs.items()):
            setattr(self, k, v)

    @classmethod
    def job_documentation(cls):
        """作业清单文档(供 --features / 内部自检)。"""
        result = []
        for sc in TrackSubClasses.sorted_by_kind(cls):
            if sc.__doc__:
                result.append('  * %s - %s' % (sc.__kind__, sc.__doc__))
            else:
                result.append('  * %s' % (sc.__kind__,))

            for msg, value in (('    必填键: ', sc.__required__), ('    可选键: ', sc.__optional__)):
                if value:
                    result.append('%s%s' % (msg, ', '.join(value)))
            result.append('')
        return '\n'.join(result)

    def get_location(self):
        raise NotImplementedError()

    def set_base_location(self, location):
        raise NotImplementedError()

    def pretty_name(self):
        raise NotImplementedError()

    def serialize(self):
        d = {'kind': self.__kind__}
        d.update(self.to_dict())
        return d

    @classmethod
    def unserialize(cls, data):
        if 'kind' not in data:
            # 未写 kind 时按键集推断: 必填键全在且没有多余键的作业类型算候选
            kinds = [subclass.__kind__ for subclass in list(cls.__subclasses__.values())
                     if all(required in data for required in subclass.__required__) and not any(
                     key not in subclass.__required__ and key not in subclass.__optional__ for key in data)]

            if len(kinds) == 1:
                kind = kinds[0]
            elif len(kinds) == 0:
                raise ValueError('未指定 kind, 且无作业类型匹配: %r' % (data,))
            else:
                raise ValueError('多个作业类型匹配 %r: %r' % (data, kinds))
        else:
            kind = data['kind']

        return cls.__subclasses__[kind].from_dict(data)

    def to_dict(self):
        return {k: getattr(self, k) for keys in (self.__required__, self.__optional__) for k in keys
                if getattr(self, k) is not None}

    @classmethod
    def from_dict(cls, data):
        return cls(**{k: v for k, v in list(data.items()) if k in cls.__required__ or k in cls.__optional__})

    def __repr__(self):
        return '<%s %s>' % (self.__kind__, ' '.join('%s=%r' % (k, v) for k, v in list(self.to_dict().items())))

    def _set_defaults(self, defaults):
        if isinstance(defaults, dict):
            for key, value in defaults.items():
                if key in self.__optional__ and getattr(self, key) is None:
                    setattr(self, key, value)

    def with_defaults(self, config):
        """套用 config.job_defaults —— 返回**新对象**, 不污染原作业。"""
        new_job = JobBase.unserialize(self.serialize())
        cfg = config.get('job_defaults')
        if isinstance(cfg, dict):
            new_job._set_defaults(cfg.get(self.__kind__))
            new_job._set_defaults(cfg.get('all'))
        return new_job

    def get_guid(self):
        """作业指纹 = sha1(位置)。上游用它当缓存主键; 本项目按 URL 粒度另存, 这里只作标识。"""
        location = self.get_location()
        sha_hash = hashlib.new('sha1')
        sha_hash.update(location.encode('utf-8'))
        return sha_hash.hexdigest()

    def retrieve(self, job_state):
        raise NotImplementedError()

    def format_error(self, exception, tb):
        return tb

    def should_ignore_error(self, exception) -> bool:
        return False


class Job(JobBase):
    """普通作业基类(可带过滤链/标签/开关)。"""

    __required__ = ()
    # 注意: 上游有 'diff_tool'(外部 diff 命令), 这里**不移植** —— 执行面红线。
    __optional__ = ('name', 'filter', 'max_tries', 'compared_versions', 'diff_filter',
                    'enabled', 'treat_new_as_changed', 'user_visible_url', 'tags')

    def matching_tags(self, tags: Iterable[str]) -> set:
        return (self.tags or frozenset()).intersection(tags)

    def location_is_url(self):
        return bool(re.match("^([a-zA-Z0-9+.-]+)://", self.get_location()))

    def pretty_name(self):
        return self.name if self.name else self.get_location()

    def is_enabled(self):
        return self.enabled is None or self.enabled

    @property
    def tags(self) -> Optional[FrozenSet[str]]:
        return self._tags

    @tags.setter
    def tags(self, value: Optional[Iterable[str]]):
        if value is None:
            self._tags = None
        elif isinstance(value, str):
            self._tags = frozenset([value])
        else:
            self._tags = frozenset(value)
