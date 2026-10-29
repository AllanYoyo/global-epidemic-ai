# -*- coding: utf-8 -*-
"""fetchlib —— urlwatch 引擎内核的移植版(零第三方依赖)。

来源: 疫见全球_plus/urlwatch-master (urlwatch 2.29, thp/urlwatch master)
移植者: 疫见全球 取数层重写方案(见 docs/ 与 AGENTS.md「取数内核」一节)

为什么不是 pip install urlwatch:
  疫见全球 的红线是「依赖仅 openpyxl/python-docx/flask, 全部优雅降级, 不加硬依赖」
  (AGENTS.md)。urlwatch 的引擎骨架本身纯 stdlib, 但三处第三方依赖会破红线:
    requests      → 传输层可注入(默认 urllib, 生产用 curl 四层降级)
    lxml/cssselect → 放弃 css/xpath 过滤器, 关注面压缩改用 stdlib html.parser
    minidb       → sqlite3
  故只移植骨架, 依赖换 stdlib 等价实现。

模块分工:
  metaclass.py  TrackSubClasses —— 插件自注册元类
  jobbase.py    JobBase / Job —— 作业模型
  urljob.py     UrlJob —— 单 URL 条件请求抓取
  filters.py    FilterBase + html2text/grep/inverse-grep/strip/sha1sum —— 关注面指纹
  cache.py      CacheStorage + SQLiteCacheStorage —— URL 粒度抓取状态与快照
  state.py      JobState —— 单 URL 取数状态机与 diff
  runner.py     四态判定 + 异常隔离的并发执行
"""
