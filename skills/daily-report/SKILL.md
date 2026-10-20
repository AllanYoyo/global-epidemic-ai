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

2. 推送办公渠道(**第 0 步: 先声明推送范围, 不得跳过**):

   用户明确要求"反馈必须先在原渠道完成再扩散到其它渠道"。**推送范围由发起方声明, 不由你临场判断**。

   - **来自单一渠道会话**(用户在微信/钉钉/邮箱里发起的扫描、测试或补发请求):
     **只能先发那个渠道**, 等用户在该渠道确认后, 再发其余渠道:

     ```bash
     # 阶段 1: 只发审阅渠道(渠道名按实际渠道替换: wechat / dingtalk / mail, 也认中文别名)
     python scripts/push_report.py --date <YYYY-MM-DD> --review-channel wechat
     # 阶段 2: 用户在原渠道确认"可以发"之后, 才发完其余渠道
     python scripts/push_report.py --date <YYYY-MM-DD> --review-approved
     ```

     阶段 1 会自动写**推送范围锁**(`data/reports/.push-scope/<日期>.lock`, scope=review),
     本轮任何 `--broadcast` 都会被拒绝 —— 不会再扩散到钉钉/邮箱。

     ⚠ **红线**: 未经原渠道用户确认, 不得执行阶段 2, 也不得改用 `--broadcast`
     (没有调度路径的全量范围声明时, `--broadcast` 会被拒绝并退出码 1)。
     **当日已推送过**(被幂等跳过)时, 补发要 `--force --review-channel <渠道>`,
     仍然只发该渠道。
   - **来自定时任务**(cron `run_scan.sh`, 非渠道会话): 走
     `scripts/run_scan.sh <模式> --broadcast`(它会先写全量范围锁), 不要自己拼 `--broadcast`。
     注意: `run_scan.sh --broadcast` **只在 cron 上下文里成立** —— 手动/会话触发时会被
     拒绝并退出码 2(手动补跑与 cron 共用入口, 允许它自称定时任务就等于把"谁发起的"
     又交回给调用方, 10-20 的钉钉误推正是这么来的)。
   - **本轮范围未声明**(例如手工跑了 `scripts/run_scan.sh policy`): **不要执行 `--broadcast`**;
     若本轮确实是某个渠道的会话, 按上面阶段 1 的写法只发那个渠道; 否则只生成日报, 不推送。

   其它用法:

   ```bash
   python scripts/push_report.py --date <YYYY-MM-DD> --dry-run   # 只预览, 不发送
   python scripts/push_report.py --date <YYYY-MM-DD> --force     # 当日已推送过, 强制重发
   python scripts/diagnose_push.py                              # 推送链路现场诊断(只读)
   ```

   - **不指定任何范围时脚本不会推送**(退出码 2 并提示三种写法)。这是有意的:
     扩散必须显式声明, 免得"忘了指定渠道"被当成"发所有渠道"。
   - 日报当天已推送过(有 `data/reports/.pushed/<日期>.flag`)会被幂等跳过, 补发加 `--force`。
   - `run_scan.sh` 未配 `HERMES_RUN_CMD` 时的 fallback 走"待执行提醒"路径, 范围与本轮声明一致
     (渠道会话只提醒审阅渠道; 定时任务才群发)。
   - 用户要求"发邮件给…"/"邮件通知"时, 不走本脚本: 直接调用 Hermes 的 `tuta-webmail` 技能发送
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
