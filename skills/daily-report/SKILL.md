---
name: daily-report
description: 生成每日动植物检疫政策监测日报(Markdown + Word 简报 + Excel)。默认服务于 policy 分支: 当用户要"生成今日政策日报""汇总各国检疫政策变化"时使用;旧疫情日报用 --outbreak 显式触发。按政策变化、动作方向、对华影响与来源索引输出到 data/reports/, 并可推送到企业微信/钉钉/邮箱。
---

# daily-report · 政策监测日报

## 目标

产出一份 3 分钟能读完、每个结论都能点回官方来源的日报。

## 前置检查

- 生成前快速检查:今日新增事件是否走完 抽取→核验→研判?若有高风险事件缺 `china_risk`,先提示用户跑 `epidemic-verification` / `china-risk-analysis`,或在日报中如实标注缺口。

## 步骤

1. 生成:

   ```bash
   python scripts/report.py --policy --date <YYYY-MM-DD> --excel --docx
   ```

   产出 `data/reports/<日期>-policy-report.md` + `.xlsx`(无 openpyxl 时降级 CSV) + `.docx` 政策监测 Word 简报(无 python-docx 时跳过)。

2. (可选)推送办公渠道:

   ```bash
   python scripts/push_report.py --date <YYYY-MM-DD>   # 企业微信/钉钉/邮箱(SMTP), 未配置自动跳过; --dry-run 预览
   ```

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
