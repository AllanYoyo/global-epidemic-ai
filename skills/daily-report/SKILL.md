---
name: daily-report
description: 生成每日动植物检疫政策监测日报(Markdown + Word 简报 + Excel)。当用户要"生成今日政策日报""汇总各国检疫政策变化"时使用。按政策变化、动作方向、对华影响与来源索引输出到 data/reports/, 并可推送到企业微信/钉钉/邮箱。
---

# daily-report · 政策监测日报

## 目标

产出一份 3 分钟能读完、每个结论都能点回官方来源的日报。

## 前置检查

- 生成前快速检查:今日新增政策是否走完 抽取→核验→影响研判?若有高影响政策缺 `impact_level`,先在政策核验与 policy-impact 环节补齐,或在日报中如实标注缺口。

## 步骤

1. 生成:

   ```bash
   python scripts/report.py --date <YYYY-MM-DD> --excel --docx
   ```

   产出 `data/reports/<日期>-policy-report.md` + `.xlsx`(无 openpyxl 时降级 CSV) + `.docx` 政策监测 Word 简报(无 python-docx 时跳过)。

2. 推送办公渠道(**第 0 步: 先判定渠道, 不得跳过**):

   用户明确要求"反馈必须先在原渠道完成再扩散到其它渠道"。**推送前必须先回答一个问题:
   本轮请求来自哪个渠道?**

   - **来自单一渠道会话**(用户在微信/钉钉/邮箱里发起的扫描、测试或补发请求):
     **只能先发那个渠道**, 等用户在该渠道确认后, 再发其余渠道:

     ```bash
     # 阶段 1: 只发审阅渠道(渠道名按实际渠道替换: wechat / dingtalk / mail, 也认中文别名)
     python scripts/push_report.py --date <YYYY-MM-DD> --review-channel wechat
     # 阶段 2: 用户在原渠道确认"可以发"之后, 才发完其余渠道
     python scripts/push_report.py --date <YYYY-MM-DD> --review-approved
     ```

     ⚠ **红线**: 未经原渠道用户确认, 不得执行阶段 2, 也不得改用不带
     `--review-channel` 的命令 —— 那样会把日报发到所有渠道(钉钉/邮箱也会收到),
     等于绕过"先在原渠道完成"的要求。**当日已推送过**(被幂等跳过)时, 补发要
     `--force --review-channel <渠道>`, 仍然只发该渠道。
   - **来自定时任务**(cron `run_scan.sh`, 非渠道会话): 全量推送, 需**显式** `--broadcast`
     `python scripts/push_report.py --date <YYYY-MM-DD> --broadcast`。

   其它用法:

   ```bash
   python scripts/push_report.py --date <YYYY-MM-DD> --dry-run   # 只预览, 不发送
   python scripts/push_report.py --date <YYYY-MM-DD> --force     # 当日已推送过, 强制重发
   ```

   - **不指定任何范围时脚本不会推送**(退出码 2 并提示三种写法)。这是有意的:
     扩散必须显式声明, 免得"忘了指定渠道"被当成"发所有渠道"。
   - 日报当天已推送过(有 `data/reports/.pushed/<日期>.flag`)会被幂等跳过, 补发加 `--force`。
   - `run_scan.sh` 未配 `HERMES_RUN_CMD` 时的 fallback 走 `--broadcast --message` 路径
     (面向所有渠道的运维提醒)。但**存在待审标记时**只发审阅渠道。
   - 用户要求"发邮件给…"/"邮件通知"时, 不走本脚本: 直接调用 Hermes 的 `tuta-webmail` 技能发送
     (主题纯中文无 emoji, 正文用日报"五问"要点转纯文本; Tuta 免费版无附件, 需附件改用 SMTP 渠道)。

   用户要求"发邮件给…"/"邮件通知"时, 不走本脚本: 直接调用 Hermes 的 `tuta-webmail` 技能发送
   (主题纯中文无 emoji, 正文用日报"五问"要点转纯文本; Tuta 免费版无附件, 需附件改用 SMTP 渠道)。

3. **交付前复核**(逐项确认):
   - "立即关注"事件是否都有官方来源链接与研判依据?
   - 五问速览是否都能从正文找到答案?
   - 待核实栏是否如实反映未完成核验的事件?
   - 数字(事件数/国家数)与表内一致?

4. 向用户交付:报告路径 + 一句话导读(今天最值得注意的 1-3 件事)。

## 扩展(可选)

- 需要 PPT 周报时,依据事件库另出(Word 简报已内置于 `report.py --docx`),内容与来源保持一致。
- 用户要"周报"时,把 `--date` 换成周期起点并调整标题,模板结构不变。

## 红线

- 不得删改事件数据来"让报告好看";缺口如实呈现。
- 报告末尾免责声明保留:AI 辅助生成、仅供情报参考。
