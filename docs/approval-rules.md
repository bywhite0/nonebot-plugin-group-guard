# 审批与来源分类

审批判定与来源统计分别配置。审批规则的名称只描述命中条件，不是成员来源。

## 配置契约

`approval` 全局与 `approval.group_rules[群号]` 均支持：

- `approval_mode`：`legacy` / `rules` / `manual`，默认 `legacy`，兼容原分类关键词配置。
- `accept_rules`：按顺序匹配的允许规则列表。每条包含 `name`、`match`（`exact` 或 `contains`）、`values`（非空字符串列表）、`case_sensitive`（默认 false）、`source`（可选来源名称，默认 null）。同一列表规则名称不能重复。
- `source_statistics`：可选布尔。未指定时仅 `legacy` 模式默认启用；`rules` 和 `manual` 默认不启用来源统计。
- 原 `accept_categories`、`reject_key` 保留；未覆盖字段继承全局。

`rules` 模式先检查拒绝关键词，再按顺序匹配允许规则；没有命中交人工，不自动拒绝。`exact` 去除两端空白后完整匹配，`values` 多项表示任意一项正确。`contains` 适用于关键词能归纳的开放答案。`manual` 模式所有申请留人工，不自动批准或拒绝。

申请内容先从明确的 `问题：…\n答案：…` 格式中提取回答；只有回答参与匹配。只有问题、格式歧义或多段答案时交人工。纯答案文本可直接匹配；不递归解析回答里伪造的标签。

`source` 是显式映射，不从规则名称或固定答案推断。新记录使用可空的 `source_category`，另存 `approval_method`、`approval_rule` 与审批原因；空来源不计入来源分类图。旧记录的 `category` 按原值兼容，不批量改写。

规则模式自动审批通知不回显完整申请回答，避免固定口令在群内公开；显式配置了 `%comment%` 的自定义模板仍可能主动展示该字段。

来源统计关闭不影响审批或入群后人机验证。关键词审核与入群后极验/算术验证是独立流程。
