# 输出约定

自然语言任务优先用简明表格：输入 ID / URL、PQ 标签、页面目的与决定性理由、证据来源。不要默认输出各维度分数或复杂检查矩阵；PQ 不是维度相加。

需要机器读取时，一条输入一条 JSON 记录；批量沿用输入 ID、原始 URL、原顺序。重复项不擅自删除，可复用同一时点已确认的页面／声誉证据，但不能只按域名给所有页面同分。提供跳转后 URL 时同时保留原 URL。

## 必要字段

| 字段 | 内容 |
|---|---|
| id | 原始 ID；没有 ID 时可使用原序号，但说明是生成 ID。 |
| url | 原始输入 URL。 |
| status | `rated`、`not_rated` 或 `insufficient_evidence`。 |
| rating | 九个合法 PQ 标签之一；未评分为 null。 |
| score | 标签的本地顺序编码；未评分为 null。 |
| reason | 简明解释页面目的、决定性证据、适用档位及关键例外。未评分时解释无法评分原因。 |
| evidence | 实际查阅的来源与观察，格式为 `{source, observation}`；source 可为网址、已提供材料定位或真实工具观察记录。独立声誉和页面自身证据应可区分。 |

| rating | score |
|---|---:|
| Lowest | 0 |
| Lowest+ | 0.5 |
| Low | 1 |
| Low+ | 1.5 |
| Medium | 2 |
| Medium+ | 2.5 |
| High | 3 |
| High+ | 3.5 |
| Highest | 4 |

批次级记录实际使用的 `skill_version`（当前为 `1.2.0`）、`rating_mode: page_quality`、`task_context`（默认 `standalone_pq`；联合任务为 `combined_nm`）、实际观察日期／时区、`evidence_mode`（`live_page` 或 `supplied_page_evidence`）。混合使用时逐证据注明来源与观察时间。按需要补充 `flags`、`limitations`、`final_url`，但不要为了填字段编造未知信息。reason 中应有可理解的目的和必要 YMYL 说明，无需额外规定一组评分之外的维度数值。

`not_rated` 用于适用任务规则的退出／N/A，独立 PQ 原因可明确写 Porn、Foreign Language、Did Not Load、Restricted / Inaccessible MC；联合任务按 page-types 的对应规则处理。`insufficient_evidence` 用于缺页面／背景／关键验证材料或工具限制而无法形成有意义判断。两者都不计入 Lowest 分布；不能填 0、Medium 或“低置信 High”代替弃权。

已完成声誉调查但没有找到信息，可以在 reason 或 evidence 记录此事实；无声誉不是强制弃权。未做过调查不能写“无负面声誉”。证据用准确观察或短引文，引用可定位，遵守来源的引用限制；不记录隐私信息或密钥。

如用户明确需要五档汇总，可另商定汇总映射并保留原始九档；不要暗中舍入中间档、强制档位比例或将编码当等距测量。
