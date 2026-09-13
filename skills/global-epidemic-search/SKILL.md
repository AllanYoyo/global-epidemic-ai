---
name: global-epidemic-search
description: 每日全球动植物疫情侦察。当用户要求"搜索/收集全球疫情""看看今天有什么新疫情""疫情雷达扫描",或作为"生成今日疫情日报"流程的第一步时使用。按 watchlist 病害清单检索 WOAH/FAO/WHO/EPPO 等官方源与 ProMED 等早期信号源,把每条命中连同 URL 存档到 data/raw/。
---

# global-epidemic-search · 全球疫情侦察

## 目标

为一个运行日(默认最近 24-48 小时,首次运行可放宽到 7 天)收集全球动植物疫情线索,形成带出处的原始情报包,交给 `epidemic-extraction` 抽取。

## 输入

- `config/sources.yaml`:`runs`(运行档位)、`sources`(各源 schedule 档位)、`watchlist`(core/general 分级)与 `search_queries`(检索模板)。

## 运行档位(定时扫描时先按档位收敛范围)

本技能可能由 `scripts/run_scan.sh am|pm` 定时触发。开工前先读 `runs` 定义, 按本次档位过滤检索范围:

| 档位 | 检索源(schedule) | watchlist | 说明 |
|---|---|---|---|
| am 晨扫 | am 与 am_pm | core + general(全部) | 全量, 完整走五段流程 |
| pm 晚扫 | 仅 am_pm | 仅 core | 增量, 完成后刷新当日日报 |
| weekly | weekly | core + general | 每周一, 并入当次 am 一起跑 |
| passive | 不主动检索 | – | 中文媒体等, 仅在核验时作交叉回溯 |

用户口头触发且未提及档位时, 默认按 am 处理。

## 步骤

1. **定窗口**:确认时间窗(默认今天;用户说"补搜上周"则调整)。运行标识为当天日期 `YYYY-MM-DD`。
2. **官方源优先**:对 watchlist 逐个病害,用 `search_queries.official` 模板检索(英文病名 + 国际源;中文病名 + 国内源)。动物病害重点查 WOAH WAHIS / FAO / 农业农村部;植物病害重点查 EPPO / IPPC / NAPPO / FAO 沙漠蝗。
3. **早期信号**:用 `search_queries.early_signal` 查 ProMED、行业媒体;中文媒体线索用 `search_queries.china`。
4. **逐条存档**:每条有价值的命中——
   - 能直接访问的网页:`python scripts/collect.py --url "<URL>" --title "<标题>"`(存到 `data/raw/<日期>/`);
   - 搜索结果摘要本身:写入 `data/raw/<日期>/search-log.md`(每行:病害 | 来源 | 标题 | URL | 摘要 | 命中查询)。
5. **产出清单**:汇总 `data/raw/<日期>/raw-findings.md`:本日命中总数、按病害/国家分组、哪些疑似新事件、哪些是旧事件的后续(标 follow-up)。

## 红线

- 每条情报必须带 URL 与抓取时间;没有出处的线索不进入下一环节。
- Tier 3(自媒体/不知名网站)只作线索,单独标注"待回溯原始出处"。
- 控制节奏:单次运行有效命中约 20 条即收手,优先深度(点开官方源)而非广度。
- WAHIS 等站点打不开时,改用 `site:` 限定搜索或官方新闻页,不要卡死在单一入口。

## 输出

`data/raw/<日期>/`:raw-findings.md、search-log.md、若干存档文件(HTML/文本 + meta)。
→ 交给 `epidemic-extraction`。
