# 疫见全球 · 外国政府动植物检疫政策监测

> 本分支只收集外国政府和国际组织发布的动植物疫情管控政策变化。
> 重点包括暂停/禁止/恢复/放宽进口、检疫要求、SPS 通报、区域化、移动管控、免疫/扑杀政策。
> 中国海关总署不作为政策记录采集源,只在政策影响研判时按需作为背景。

## 生产链路

```text
外国官方公告 / WTO ePing SPS 通报
        ↓
 global-policy-search       政策变化侦察与原文存档
        ↓
 policy-extraction          结构化政策记录
        ↓
 policy-verification        官方出处与政策状态核验
        ↓
 policy-impact              对华影响研判
        ↓
 policy-report              Markdown + Word + Excel + 推送
```

生产数据合同只有 `record_type=policy`。未来如果重新建设疫情检测,另建数据合同与管线。

## 影响研判

政策记录不使用“疫情高风险/中风险”的旧概念,而使用：

- `impact_type`: 约束 / 机会 / 中性;
- `impact_level`: 高影响 / 中影响 / 低影响;
- `china_relevance`: 直接涉及中国 / 间接影响 / 暂无明显关联;
- `recommended_action`: 核查准入、跟踪法规、提醒企业或常规记录;
- `policy_status`: 草案、已发布未生效、已生效、已解除。

只有中高影响政策需要完整贸易背景研判,低影响政策做基础事实判断即可。

## 快速开始

```bash
pip install openpyxl python-docx

# 政策公告存档
python scripts/collect.py --url "https://example.com/notice" --title "外国官方检疫公告"

# 抽取后的政策 JSON 入库(重复入库按 event_id 自动合并, 不覆盖已完成的核验/研判; 整行替换需显式 --replace)
# 默认只收发布时间在近 7 天内的新记录, 检索撞到的旧公告页会被"旧闻拒收"(回补历史传 --max-age-days N, 0 关闭)
python scripts/normalize.py --input data/raw/<日期>/extracted-policy.json \
  --out data/events/<日期>/ --db

# 写回影响研判(四维齐全时 score/level/focus 由代码按加权合同推导并强校验)
python scripts/risk.py --event-id <id> \
  --impact-type 约束 --china-relevance 间接影响 \
  --action "立即核查我国相关进口准入" \
  --rationale "依据官方措施与贸易背景" \
  --dim trade=4 --dim biosecurity=3,response=3,alignment=2

# 生成政策日报、Word 和 Excel
python scripts/report.py --date $(date +%F) --excel --docx

# 推送政策日报(企业微信/钉钉/邮箱, 配了哪个发哪个; 建议先 --dry-run 预览)
python scripts/push_report.py --date $(date +%F) --dry-run
# 渠道会话(先在原渠道给用户看, 确认后再发其余渠道):
python scripts/push_report.py --date $(date +%F) --review-channel weixin
python scripts/push_report.py --date $(date +%F) --review-approved
# 定时任务的全量推送(需先由调度路径声明全量范围, 见"推送范围锁"):
scripts/run_scan.sh policy --broadcast
# 同一天只发一次(幂等), 重发需 --force

# 启动政策监测面板
python webapp/app.py
```

演示数据：

```bash
python scripts/normalize.py --input examples/sample-policy-events.json --db --max-age-days 0  # 演示数据为历史日期, 关掉旧闻拦截
python scripts/report.py --excel --docx
```

## 输出

| 输出 | 内容 |
|---|---|
| Markdown | 政策速览、动作方向、政策状态、影响类型/等级、中国关联、建议动作、来源 |
| Word | 政策变化表、收紧/调整/高影响详情、影响综述、待核实和来源索引 |
| Excel | “政策变化台账”工作表，支持筛选、冻结表头、换行和打印 |
| 网页 | 政策影响地图、政策台账、动作/领域/影响类型/核验筛选和政策报告下载 |
| 推送 | 当日新增摘要：**重点关注**(中影响/收紧) + 其他新增, 每条附原文出处链接 |

## 定时运行

```bash
scripts/run_scan.sh policy --broadcast   # 政策扫(建议每日 07:30): 检索→抽取→核验→研判→日报→全量推送
```

扫描完成后自动用 push_report.py 推送当日日报。另有 `am`(晨扫全量, 06:30) 和 `pm`(晚扫增量, 18:00) 两档, 见 `config/crontab.example`。网页生成按钮默认执行政策日报、Word 和 Excel；自定义生成命令使用 `RADAR_POLICY_GENERATE_CMD`。

### 推送范围锁(2026-10-20 起)

「本轮日报允许发到哪些渠道」不再由 Agent 临场判断，而是**由发起方声明并落盘成锁**(`data/reports/.push-scope/<日期>.lock`)，`push_report.py` 读锁执行：

| 场景 | 发起方式 | 效果 |
|---|---|---|
| 渠道会话(微信/钉钉/邮箱里提的要求) | `scripts/run_scan.sh policy --review-channel weixin` 或 `python scripts/push_report.py --date X --review-channel weixin` | 只发该渠道；用户确认后 `--review-approved` 才发其余渠道 |
| 定时任务 | `scripts/run_scan.sh policy --broadcast`（**必须在 cron 上下文里跑**） | 全量推送 |
| 都没声明 | `scripts/run_scan.sh policy` | cron 上下文自动按定时任务全量；其它上下文**不扩散**（`--broadcast` 一律拒绝） |
| 人工运维全量补发 | `python scripts/push_scope.py --date X --broadcast --source manual:运维补发` 后再 `python scripts/push_report.py --date X --broadcast` | 全量推送，留痕可审计（`--source` 必须写明依据） |

`push_report.py --broadcast` **不再自声明**：没有调度路径的全量范围声明就拒绝执行（退出码 1），避免"谁喊一声都能发到所有渠道"。

### 两类推送渠道

| 类型 | 渠道 | 依赖 |
|---|---|---|
| webhook 型(脚本自己 POST) | 企业微信群机器人 / 钉钉机器人 / 邮箱 | `WECHAT_WEBHOOK` / `DINGTALK_WEBHOOK` / `SMTP_*` |
| Hermes 投递型(借 `hermes send`) | **微信**(个人微信会话) / 飞书 | `hermes` CLI + Hermes 已配置该平台 |

生产现场事实：`.env` 里**没有** `WECHAT_WEBHOOK`，用户说的"微信会话"是 Hermes 的个人微信通道（`WEIXIN_*`）——脚本自己 POST 发不进去，只有 `hermes send --to weixin` 能把日报送进那个会话（并可顺带带上 Word 报告）。因此渠道名上：**"微信"=`weixin`（个人微信/Hermes）、"企业微信"=`wechat`（群机器人 webhook）**，两者别混。

Hermes 渠道**默认不参与定时全量**（免得日报突然刷屏用户微信）；要把日报也送到那里，用 `HERMES_PUSH_CHANNELS=weixin,feishu` 显式打开。

## 部署到 VPS

```bash
mkdir -p /opt/global-epidemic-ai/{data/raw,data/events,data/reports,database,logs}
cd /opt/global-epidemic-ai && git clone <你的GitHub仓库> repo && cd repo
pip3 install -r requirements.txt

export EPIDEMIC_DATA_DIR=/opt/global-epidemic-ai/data
export EPIDEMIC_DB=/opt/global-epidemic-ai/database/epidemic.db

ln -sfn /opt/global-epidemic-ai/repo/skills/global-policy-search ~/.hermes/skills/global-policy-search
ln -sfn /opt/global-epidemic-ai/repo/skills/policy-verification ~/.hermes/skills/policy-verification
ln -sfn /opt/global-epidemic-ai/repo/skills/daily-report ~/.hermes/skills/daily-report
```

重启/重载 Hermes 后，使用"生成今日政策日报"触发完整链路。

### 更新生产代码(重要)

仓库在 VPS 上是 `/opt/global-epidemic-ai/repo` 这个 clone 的工作树，**不会自动更新**——不 pull 就一直跑旧逻辑(2026-10-20 钉钉误推事故排查时，生产跑的还是加固前的旧代码)。更新用：

```bash
bash scripts/deploy_update.sh policy     # git pull --ff-only + 装依赖 + 跑一遍链路自检
python3 scripts/diagnose_push.py          # 单独诊断：代码版本/范围锁/crontab/日志
```

`diagnose_push.py` 是只读的现场诊断脚本：一次打印运行代码 commit（与远端是否一致）、当日范围锁与待审/已推标记、渠道配置、crontab 里是否显式声明了推送范围、调度日志尾部（有无 `scope=` 标记可区分新旧版 `run_scan.sh`）。

## 目录

```text
ARCHITECTURE.md                    架构与数据流、schema、Hermes 调度
CONTRIBUTING.md                    新增情报源 / 改提示词 / PR 清单
TROUBLESHOOTING.md                 常见故障排查
docs/event-schema.md               数据字段权威定义
skills/global-policy-search/       政策变化侦察
skills/policy-verification/        政策官方出处核验
skills/daily-report/               政策日报交付
prompts/policy-extraction.md       政策抽取
prompts/policy-impact.md           对华影响研判
scripts/collect.py                 原文存档(可溯源链第一环)
scripts/normalize.py               政策数据合同与入库
scripts/risk.py                    政策影响写回
scripts/report.py                  Markdown/Word/Excel 入口
scripts/report_docx.py             政策 Word 简报
scripts/push_report.py             政策推送
scripts/push_scope.py              推送范围锁(声明本轮允许发到哪些渠道)
scripts/diagnose_push.py           推送链路现场诊断(只读)
scripts/deploy_update.sh           生产更新(pull + 依赖 + 自检)
scripts/buildinfo.py               运行代码版本指纹(推送内容尾部标注 commit)
webapp/app.py                      政策监测面板
config/sources.yaml                情报源与政策检索模板
```

## 免责声明

本系统由 AI 辅助生成政策情报,所有记录附原始来源;核验状态、影响等级和建议动作仅供情报参考,不构成决策或执法依据。正式口岸措施以有关官方公告为准。
