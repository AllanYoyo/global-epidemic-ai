---
name: china-risk-analysis
description: 从进境动植物检疫视角评估疫情对中国的潜在风险。当需要判断"某疫情是否/如何影响中国""风险等级""是否需要警示或禁令"时使用。先检索对华贸易与海关总署现有措施,再按四维度打分(prompts/risk-analysis.md),结论经 scripts/risk.py 写回事件。
---

# china-risk-analysis · 对华风险研判

## 目标

把 `verified` 事件变成一个有依据、可比较的对华风险结论。

## 输入

- 已核验事件中尚无 `china_risk` 的:

  ```bash
  python scripts/risk.py --list-pending
  ```

## 步骤

1. **补背景**(每条事件检索量控制在 2-4 次):
   - 贸易关联:`中国 自<国家> 进口 <宿主/产品>`、海关总署"准予进口"名录相关页面;
   - 现有措施:`海关总署 <病害> 警示通报/禁止进口 <国家>`;
   - 自然传播:视病害查候鸟迁徙(禽流感)、沙漠蝗迁移路径、媒介分布等。
2. 按 `prompts/risk-analysis.md` 打分:commodity×0.35 + pathway×0.30 + impact×0.25 + measures×0.10,得出 `level` 与 `focus`(立即关注/持续观察/常规记录)。
3. 结论写回:

   ```bash
   # 方式一: 便捷写入(适合单条)
   python scripts/risk.py --event-id <event_id> --level high --score 3.8 --focus 立即关注 \
     --rationale "..." --trade "..." --gacc "..."

   # 方式二: 批量写 JSON 文件(结构见 prompts/risk-analysis.md)
   python scripts/risk.py --from-file risk-batch.json
   ```

4. 汇总当日研判要点(哪些立即关注、为什么),供 `daily-report` 的"对华风险研判综述"使用。

## 红线

- 贸易与措施信息找不到就写"背景资料未提及",**禁止编造进口数据或公告**。
- 官方已发禁令的既有疫情,`focus` 最高只能"持续观察"。
- 结论属于情报参考:`rationale` 必须能看出事实依据,便于复核。

## 输出

写回 `china_risk` 的事件 → 全部就绪后交给 `daily-report`。
