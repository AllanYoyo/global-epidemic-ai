---
name: epidemic-verification
description: 对疫情事件做官方来源核验与多源交叉验证。当事件处于 unverified/single_source 状态需要定级、需要判断某条疫情消息真假、需要为事件找 WOAH/FAO/官方出处时使用。判定标准:verified / single_source / unverified / false_positive,并记录核验轨迹。
---

# epidemic-verification · 核验

## 目标

给每条事件一个可信等级,并把官方出处顶到 `source` 位置。

## 输入

- `verification_status ∈ {unverified, single_source}` 的事件(先查 `data/events/` 下对应日期目录,或查 SQLite)。

## 步骤

1. 对每条事件生成针对性检索(英文优先):`"{disease_name_en}" "{country_en}" immediate notification`、`site:wahis.woah.org ...`、`EPPO reporting service ...`;国内事件检索农业农村部/海关总署发布。
2. 逐条对照 `prompts/verification.md` 的判定标准与权威级差,产出核验结论。
3. 处理结论:
   - 找到 Tier 1 官方源 → 用官方信息修正事件字段,`source` 换成官方源,原来源进 `cross_sources`,`verification_status=verified`;
   - ≥2 个独立 Tier 2 一致 → 保持 single_source,`verification_notes` 写"多源一致(非官方)";
   - 检索无果 → 保持 unverified,把查过的关键词/URL 记入 `checked_urls`(留轨迹);
   - 关键事实矛盾 → `verification_status=false_positive`,在 `verification_notes` 列出矛盾证据,不删除事件。
4. **写回**(务必加 `--update` 合并,保留已有字段):

   ```bash
   python scripts/normalize.py --input updated-events.json --update --db
   ```

5. 当日核验台账:`data/reports/<日期>/verification-log.md`(事件ID | 原状态 | 新状态 | 依据 | 检索词)。

## 红线

- "没找到官方来源"≠"假消息",只能停留在 unverified。
- false_positive 必须有可复核的矛盾证据,否则宁可降级为 unverified。
- 不改采集性字段(数量、地点等),除非官方源给出新值(记入 verification_notes)。

## 输出

状态升级后的事件 → `verified` 的交给 `china-risk-analysis`;`unverified` 汇入日报"待核实"栏。
