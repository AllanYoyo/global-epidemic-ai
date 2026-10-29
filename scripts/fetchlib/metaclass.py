# -*- coding: utf-8 -*-
"""TrackSubClasses 元类 —— 移植自 urlwatch 2.29(thp/urlwatch master)。

来源: urlwatch-master/lib/urlwatch/util.py:43-81

为什么移植而不是 pip 装 urlwatch:
  疫见全球 的红线是「依赖仅 openpyxl/python-docx/flask, 全部优雅降级, 不加硬依赖」
  (见 AGENTS.md)。urlwatch 的引擎骨架本身是纯 stdlib, 但它的三处第三方依赖
  (requests / lxml+cssselect / minidb) 会破坏这条红线 —— 所以这里只移植**骨架**,
  依赖换成 stdlib 等价实现。

这个元类解决的是「插件自注册」:
  子类只要在类体里写 __kind__ = 'xxx'(即 YAML 配置键), 类一创建就自动注册进
  基类的 __subclasses__ 字典, 无需改任何中心注册表。
  __required__ / __optional__ 会在继承时自动向基类合并(子类只写增量部分)。

注意: 注册发生在**导入时**。hooks.py / policy_fetch.py 里定义的类与内置类共享
同一个全局注册表 —— 这是特性(用户可扩展), 也是潜在踩坑点(同名 __kind__ 会覆盖)。
"""
import logging

logger = logging.getLogger(__name__)


class TrackSubClasses(type):
    """一个元类, 存储子类名到子类的映射(在基类里)。"""

    @staticmethod
    def sorted_by_kind(cls):
        """按 __kind__ 排序的子类列表 —— 文档生成与 --features 输出用。"""
        return [item for _, item in sorted((it.__kind__, it) for it in cls.__subclasses__.values())]

    def __init__(cls, name, bases, namespace):
        for base in bases:
            if base == object:
                continue

            for attr in ('__required__', '__optional__'):
                if not hasattr(base, attr):
                    continue

                inherited = getattr(base, attr, ())
                new_value = tuple(namespace.get(attr, ())) + tuple(inherited)
                namespace[attr] = new_value
                setattr(cls, attr, new_value)

        for base in bases:
            if base == object:
                continue

            if hasattr(cls, '__kind__'):
                subclasses = getattr(base, '__subclasses__', None)
                if subclasses is not None:
                    logger.info('注册 %r 为 %s', cls, cls.__kind__)
                    subclasses[cls.__kind__] = cls
                    break
            else:
                anonymous_subclasses = getattr(base, '__anonymous_subclasses__', None)
                if anonymous_subclasses is not None:
                    logger.info('注册 %r', cls)
                    anonymous_subclasses.append(cls)
                    break

        super().__init__(name, bases, namespace)
