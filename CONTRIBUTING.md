# CONTRIBUTING.md · 参与指南

面向本仓库的改动手册：如何新增情报源、如何改提示词、提 PR 前查什么。系统定位与数据合同见 `AGENTS.md`，架构见 `ARCHITECTURE.md`。

## 1. 如何添加新情报源

全部在 `config/sources.yaml` 完成，**不改代码**。

### 1.1 入围标准

- 只收**外国政府/国际组织**官方源；中国海关总署公告不进源清单（仅在影响研判时作背景）。
- 优先 `tier: 1`（政府公报、检疫机构公告页）、`tier: 2`（WTO ePing、WOAH/FAO/IPPC 等国际组织）；新闻媒体不进 `sources`，核验时只当线索。
- URL 填**检索入口页**（列表页/搜索页），站点改版后由搜索重新定位，不追求永久链接。

### 1.2 添加步骤

1. 在 `sources:` 下新增条目，字段与现有条目保持一致：

   ```yaml
   - id: xx-agri                      # 仓库内唯一, 用 <国家码>-<机构> 小写风格
     name: XX国农业部(XXX)             # 中文名(机构英文缩写)
     url: https://example.gov/        # 官方公告/新闻列表入口
     coverage: [measures, animal]     # 覆盖领域: measures 为政策扫必跑档, 另有 animal/plant/trade
     tier: 1                          # 1=官方公报 2=国际组织 3=权威线索源
     schedule: am                     # 档位: am(晨扫)/pm(晚扫)/am_pm(两扫都跑)/weekly(周一附加)
     lang: [en, fr]                   # 站点语言, 供检索词与核验参考
   ```

2. 若该机构对应某个受监测国家，把 `id` 挂到 `policy_watch_countries` 对应优先级条目的 `source_ids`，并在 `aliases` 里补机构英文缩写（检索词会用到）：

   ```yaml
   - {cn: 泰国, en: Thailand, aliases: [DLD, DOA], source_ids: [th-dld, th-doa]}
   ```

3. 确认 `schedule` 取值符合预期节奏：policy 扫（每日 07:30）主要按 `coverage: [measures]` + 国家优先级组合检索，不严格按单个源的 schedule 跑；`am`/`pm`/`weekly` 档由 `run_scan.sh` 对应模式驱动（weekly 仅周一晨扫附加）。
4. 若新源内容超出 `policy_keywords` 现有六类词库（组织机制/商品生物材料/活动物/植物/疫病风险/证书流程/法规标准），同步在 `policy_keywords` 补词（每条 `{cn, en, aliases[]}`），并考虑在 `policy_queries` 加检索模板。

### 1.3 新源验收

- 用 `python scripts/collect.py --url <源上某条公告URL> --title 测试` 走通存档；
- 手工按 `prompts/policy-extraction.md` 从该公告抽一条政策记录，`python scripts/normalize.py --input <json> --db` 能通过校验入库（枚举值、source.url/quote 齐全）；
- 观察 2-3 个扫描日确认命中质量，再决定是否提为 priority_1。

## 2. 如何测试新的提示词

`prompts/policy-extraction.md` 与 `prompts/policy-impact.md` 是数据合同的一部分，改动视同改合同。

1. **先读敏感区文件**：`docs/event-schema.md`（字段权威定义）、`prompts/policy-impact.md` 现行研判口径——两份 prompt 的输出约定必须与之一致。
2. **用 `examples/` 做回归样例**：`examples/sample-policy-events.json` 是标准输入样例；改动抽取 prompt 后，用旧样例喂新 prompt，比对产出 JSON 是否仍满足 `normalize.py` 校验：

   ```bash
   python scripts/normalize.py --input examples/sample-policy-events.json --out /tmp/events/ --db
   ```

   `examples/verify-patch.json` 是核验/研判写回的样例补丁。
3. **研判口径回归**：改 `policy-impact.md` 后，对同一批已核验记录重跑四维打分，用 `python scripts/risk.py --list-pending` 核对 score/level/focus 推导结果与权重合同（trade*0.35 + biosecurity*0.30 + response*0.25 + alignment*0.10）一致；四维齐全时 score/level/focus 由代码推导，prompt 里不允许出现与之冲突的手写规则。
4. **提示词改动随带三处同步**（见 AGENTS.md 数据合同节）：normalize.py、docs/event-schema.md、report.py/report_docx.py 展示层。只改了 prompt 措辞不涉及字段的，说明理由即可。

## 3. PR 检查清单

提交前逐项自查（本仓库无测试套件、无 linter，人工核对即清单本身）：

- [ ] **口径**：只涉及政策监测；未重新引入疫情事件管线；未把中国海关总署加入采集源。
- [ ] **数据合同**：改字段/枚举时，normalize.py、docs/event-schema.md、两个 prompt、report.py/report_docx.py 展示层已同步。
- [ ] **中文文案**：docstring、CLI 输出、校验报错、文档一律中文；注释中文、贴近现有 `%` 格式化风格；stdlib 优先（urllib 而非 requests）。
- [ ] **优雅降级**：新功能在缺 openpyxl / python-docx / flask 时行为合理，未引入硬依赖。
- [ ] **运行方式**：新脚本可从仓库根目录以 `python scripts/xxx.py` 运行；scripts 内部用同级导入，需要跨目录引用时照 report.py 的 `sys.path.insert` 模式。
- [ ] **数据不进门**：改动未把 `data/`、`database/`、`logs/` 下的运行时产物提交进 git。
- [ ] **幂等性**：涉及入库/推送的改动，重复执行不产生重复记录/重复消息（event_id 合并、`.pushed/<日期>.flag` 语义未被破坏）。
- [ ] **溯源要求**：涉及抽取/核验的改动仍强制 `source.url` + `source.quote`（≤120 字），未知信息落 null 不编造。
- [ ] **本地冒烟**：

  ```bash
  python scripts/normalize.py --input examples/sample-policy-events.json --db
  python scripts/report.py --excel --docx          # 缺依赖时观察 CSV 降级
  python scripts/push_report.py --date $(date +%F) --dry-run
  python webapp/app.py                             # 面板能起, events API 需 record_type=policy
  ```

- [ ] **提交信息**：conventional commits（`feat/fix/docs/chore(scope)`），风格对齐 git log。

## 4. 其他约定

- 开发在 Windows，生产是 Linux VPS：注意路径分隔符、CJK 字体（templates/base.py 按 platform 选择）差异。
- 面板只读 + 一键生成日报，勿把 webapp 裸暴露公网；events API 必须显式 `record_type=policy` 才返回数据。
- 拿不准是否该改的地方，优先看 `AGENTS.md` 的"数据合同（改动需同步多处）"与"敏感区改动前先读"两节。
