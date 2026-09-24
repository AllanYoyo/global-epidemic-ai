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

# 推送政策日报
python scripts/push_report.py --date $(date +%F)

# 启动政策监测面板
python webapp/app.py
```

演示数据：

```bash
python scripts/normalize.py --input examples/sample-policy-events.json --db
python scripts/report.py --excel --docx
```

## 输出

| 输出 | 内容 |
|---|---|
| Markdown | 政策速览、动作方向、政策状态、影响类型/等级、中国关联、建议动作、来源 |
| Word | 政策变化表、收紧/调整/高影响详情、影响综述、待核实和来源索引 |
| Excel | “政策变化台账”工作表，支持筛选、冻结表头、换行和打印 |
| 网页 | 政策影响地图、政策台账、动作/领域/影响类型/核验筛选和政策报告下载 |
| 推送 | 政策变化数、收紧数、约束/机会、高影响政策和后续建议 |

## 定时运行

```bash
scripts/run_scan.sh policy
```

建议每日 07:30 运行。网页生成按钮默认执行政策日报、Word 和 Excel；自定义生成命令使用 `RADAR_POLICY_GENERATE_CMD`。

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

重启/重载 Hermes 后，使用“生成今日政策日报”触发完整链路。

## 目录

```text
skills/global-policy-search/       政策变化侦察
skills/policy-verification/        政策官方出处核验
skills/daily-report/               政策日报交付
prompts/policy-extraction.md       政策抽取
prompts/policy-impact.md           对华影响研判
scripts/normalize.py               政策数据合同与入库
scripts/risk.py                    政策影响写回
scripts/report.py                  Markdown/Word/Excel 入口
scripts/report_docx.py             政策 Word 简报
scripts/push_report.py             政策推送
webapp/app.py                      政策监测面板
config/sources.yaml                情报源与政策检索模板
```

## 免责声明

本系统由 AI 辅助生成政策情报,所有记录附原始来源;核验状态、影响等级和建议动作仅供情报参考,不构成决策或执法依据。正式口岸措施以有关官方公告为准。
