# 疫见全球 · 全球动植物疫情智能情报雷达

> 让全球疫情信息,从"新闻"变成"风险情报"。
> 多 Agent 疫情情报系统 —— 回答五个问题:
> **哪些疫情正在发生?发生在哪里?涉及什么动植物?是否可能影响我国?哪些值得立即关注?**

## 系统架构

```text
 你
 │  "生成今日疫情日报"
 ↓
 Hermes Agent(总指挥, 依次调度 5 个 Skill)
 │
 ├─① global-epidemic-search   检索官方/专业源, 原始情报带URL存档
 ├─② epidemic-extraction      千问抽取 → 标准疫情事件
 ├─③ epidemic-verification    官方核验 + 多源交叉验证, 分级定信
 ├─④ china-risk-analysis      千问四维风险研判
 └─⑤ daily-report            日报 Markdown + Excel
 │
 ↓
 疫情事件库(JSON + SQLite) ← 每条事件可溯源到官方 URL
```

```text
 Windows PC(VS Code / Git) ──git── GitHub ──git pull── VPS
                                                /opt/global-epidemic-ai/
                                                ├── repo/       本仓库代码
                                                ├── data/       raw · events · reports
                                                ├── database/   epidemic.db
                                                └── logs/
 VPS: Hermes(~/.hermes/) + 本仓库脚本 ←─ HTTPS/API ─→ 千问 DashScope
 情报源: WOAH(WAHIS) · FAO(EMPRES-i) · WHO(DON) · IPPC/EPPO/NAPPO · ProMED · 海关总署 · 农业农村部
         + 40 余国官方检疫机构/行业媒体(来自《动物疫情信息参考网站.xlsx》, 详见 config/sources.yaml)
```

## 五个 Skill

| # | Skill | 职责 | 产出 |
|---|---|---|---|
| 1 | `global-epidemic-search` | 按 watchlist 侦察全球疫情(官方源 → 早期信号 → 中文线索) | `data/raw/` 带 URL 的原始情报 |
| 2 | `epidemic-extraction` | 千问把原始情报抽成标准事件(缺失填 null, 不编造) | `data/events/` + SQLite |
| 3 | `epidemic-verification` | 官方出处核验 + 多源交叉验证 | verified / single_source / unverified / false_positive |
| 4 | `china-risk-analysis` | 四维评分(商品关联/传入路径/后果/现有措施) | 对华风险等级 + 关注等级 |
| 5 | `daily-report` | "五问"结构日报, 每条带来源链接 | MD + Word + Excel; 可推送企微/钉钉/邮箱 |

## 快速开始

### 在 Hermes 中(推荐)

```bash
# VPS 上把 skills 装进 Hermes(或软链)
cp -r skills/* ~/.hermes/skills/
```

然后对话:**"生成今日疫情日报"** —— Hermes 按 ①→⑤ 依次调度。
也可以分步执行,如"先核验今天的事件""评估一下德国猪病的对华风险"。

### 手动管线(调试 / 演示兜底)

```bash
pip install openpyxl    # 可选: Excel 输出; 缺失时 report.py 自动降级为 CSV

python scripts/collect.py --url "https://example.com/notice" --title "某官方通报"      # ① 存档
# ② 抽取: LLM 按 prompts/extraction.md 产出 JSON 后:
python scripts/normalize.py --input data/raw/<日期>/extracted-events.json --out data/events/<日期>/ --db
python scripts/deduplicate.py                                                        # 去重合并
python scripts/risk.py --list-pending                                                # ④ 待研判清单
python scripts/risk.py --event-id <id> --level high --score 3.8 --focus 立即关注 --rationale "..."
python scripts/report.py --excel --docx                                              # ⑤ 日报 + Word 简报
python scripts/push_report.py --date $(date +%F)                                     # ⑥ 推送(企微/钉钉/邮箱)
python webapp/app.py                                                                 # ⑦ 疫情雷达面板 :8000
```

> 空库试跑可用演示数据(全部为虚构标注"演示数据"的事件):
> `python scripts/normalize.py --input examples/sample-events.json --db` → `deduplicate` → `report`。

## 界面与交付

作品呈现分三层, Hermes 仍是引擎("前端是脸, Hermes 是发动机"):

| 层 | 内容 | 入口 |
|---|---|---|
| Word 情报简报 | 五问速览 / 事件表(风险着色) / 研判综述 / 来源超链接的正式排版 | `report.py --docx` |
| 办公推送 | "五问速览 + 立即关注"卡片推到企业微信 / 钉钉 / 邮箱 | `scripts/push_report.py`(渠道见 .env.example) |
| 疫情雷达面板 | 地图打点(风险着色) + 事件库筛选 + 日报渲染/下载 + 一键生成 | `python webapp/app.py`, 默认 :8000 |

依赖: `pip3 install -r requirements.txt`。面板为只读展示, "生成今日日报"按钮只重渲染报告产物;
Agent 全流程(搜索→抽取→核验→研判)仍由 Hermes 执行。生产环境请经 Tailscale / 反向代理访问面板, 不要裸暴露公网。

### 部署到 VPS(一次性清单)

```bash
# 1) 目录 + 代码
mkdir -p /opt/global-epidemic-ai/{data/raw,data/events,data/reports,database,logs}
cd /opt/global-epidemic-ai && git clone <你的GitHub仓库> repo && cd repo
pip3 install openpyxl   # 可选(Excel); 不装自动降级 CSV

# 2) 环境变量(~/.hermes/.env 与 ~/.bashrc 各写一份)
export EPIDEMIC_DATA_DIR=/opt/global-epidemic-ai/data
export EPIDEMIC_DB=/opt/global-epidemic-ai/database/epidemic.db

# 3) Skills 挂进 Hermes(软链, git pull 即生效)
for s in global-epidemic-search epidemic-extraction epidemic-verification china-risk-analysis daily-report; do
  ln -sfn /opt/global-epidemic-ai/repo/skills/$s ~/.hermes/skills/$s
done
# 重启/重载 Hermes 重新扫描 skills

# 4) 验证(演示数据)后清理
python3 scripts/normalize.py --input examples/sample-events.json --out ../data/events/test --db
python3 scripts/deduplicate.py && python3 scripts/report.py --date $(date +%F) --excel
rm ../database/epidemic.db && rm -rf ../data/events/test ../data/reports/*
```

### 定时双扫(晨 06:30 全量 / 晚 18:00 增量)

频率与档位定义在 `config/sources.yaml` 的 `runs` 与各源 `schedule` 字段, 定时入口是 `scripts/run_scan.sh`:

```bash
# 1) 配置 Hermes 无头调用方式(~/.hermes/.env), 未配置时定时任务退化为"待执行提醒"推送
echo 'HERMES_RUN_CMD=hermes run --prompt "{PROMPT}"' >> ~/.hermes/.env
# 2) 安装定时任务(内容见 config/crontab.example)
crontab -e   # 粘贴示例中两行; 确认服务器时区为 Asia/Shanghai
# 3) 手动试跑一轮
scripts/run_scan.sh am
```

| 档位 | 时间 | 检索源 | watchlist | 产出 |
|---|---|---|---|---|
| am 晨扫 | 每日 06:30 | am + am_pm | 全部(含 general) | 日报 + 推送 |
| pm 晚扫 | 每日 18:00 | 仅 am_pm | 仅 core 核心病害 | 刷新当日日报 |
| weekly | 每周一并入晨扫 | weekly(EPPO/IPPC/沙漠蝗等) | 全部 | — |

> **注意**:与 Hermes 的会话工作目录请设在仓库根 `/opt/global-epidemic-ai/repo`,Skill 内引用的 `scripts/` `prompts/` `config/` 均为相对仓库根路径。

## 目录结构

```text
global-epidemic-ai/              本仓库(即 VPS 上的 repo/)
├── README.md
├── skills/                      5 个 Skill(Hermes 读取)
├── prompts/                     千问推理提示词(extraction / verification / risk-analysis)
├── scripts/                     确定性管线(collect / normalize / deduplicate / risk / report
│                                / report_docx / push_report)
├── webapp/app.py                疫情雷达面板(Flask: 地图 / 事件库 / 日报 / 一键生成)
├── requirements.txt
├── config/sources.yaml          情报源分级 + watchlist + 检索模板
├── templates/daily_report.md    日报模板
├── docs/event-schema.md         疫情事件数据模型(系统的"合同")
├── data/                        raw / events / reports(git 忽略, 仅保留目录骨架)
└── .env.example
```

> **疫情数据不进仓库**:仓库内 `data/` 只保留骨架;生产环境用环境变量把数据指到 repo 外(见下)。

## 数据落地

| 环境变量 | 默认(本地开发) | VPS 建议 |
|---|---|---|
| `EPIDEMIC_DATA_DIR` | `<repo>/data` | `/opt/global-epidemic-ai/data` |
| `EPIDEMIC_DB` | `<repo>/database/epidemic.db` | `/opt/global-epidemic-ai/database/epidemic.db` |
| `HTTP_USER_AGENT` | 内置默认 | 自定义 UA(抓取用) |

Hermes 自身配置(`~/.hermes/`:config.yaml / .env / skills / sessions / state.db)与业务代码互不混放。

## 设计决策

1. **Agent 干智能活,Python 干确定活**。搜索、浏览、抽取、研判交给 Hermes(站点改版不会打断管线);存储、去重、渲染交给脚本(可复现、可测试、可审计)。
2. **事件数据模型是合同**。`docs/event-schema.md` 定义字段与状态流转,`scripts/normalize.py` 是唯一权威实现;五个 Skill 之间传递的是同一种结构。
3. **核验分级,不做二值**。verified / single_source / unverified / false_positive + 核验轨迹;"没找到官方源"只降级、不定性为假消息。
4. **对华风险绑定贸易事实**。四维评分中"商品关联"权重最高,强制检索海关总署现有措施;禁止编造贸易数据。
5. **每条可溯源**。事件必须带 `source.url` + 原文引用;日报中每个结论都能点回官方出处。

## 路线图

- **第一阶段(本仓库)**:5 个 Skill + 一句话触发的日报 ✅
- **第二阶段**:cron 定时全线自动跑;WAHIS / EMPRES-i API 直连。Word 简报 / 办公推送 / 雷达面板 ✅ 已交付(见「界面与交付」)
- **第三阶段**:Word/PPT 周报;疫情地图;事件库趋势分析(复发预警、季节性)

## 检索范围扩容(今后迭代方向, 以跑通为前提)

按性价比分四步, 每一步都遵守"三问筛选"(对华贸易/边境/生态关联? 名录在管? 成本与信噪比可接受?):

1. **结构化源扩容**:RSS/接口类源(WOAH WAHIS 接口、EMPRES-i API、GLEWS、ProMED RSS、USDA APHIS 等)——直接对接零 LLM 成本的轮询器, 收益最大;
2. **周边国家与贸易伙伴官方源**:中国陆地边境 14 邻国 + 主要贸易伙伴的官方检疫机构页面, 补上传入路径维度最大的盲区;
3. **病害清单扩容**:锚定《一、二、三类动物疫病名录》与《进境植物检疫性有害生物名录》, 挑活跃扩散种扩至 30~40 个;core 病害保持 ≤5 个, general 病害按日分片轮询;
4. **多语言长尾**:俄/日/西/葡等官方检疫机构页面, 发挥千问多语阅读能力。

配套治理:新源 2 周观察期(记命中率与噪音率, 连续无产出降档或移除);源健康台账(每源最近成功时间/失败次数/命中数)上面板。

## 免责声明

本系统由 AI 辅助生成情报,所有事件附原始来源;核验状态与风险等级仅供情报参考,不构成决策或执法依据。口岸措施以海关总署等官方公告为准。
