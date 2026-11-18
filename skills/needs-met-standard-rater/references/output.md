# 输出与批量执行

## 简洁输出

默认表格列出 ID、query、URL、NM 档位、理由、证据；需要机器读取时，一条输入对应一条 JSON。不要输出维度打分表或把内部检查过程变成新指标。

```json
{
  "id": "case-1",
  "query": "原始查询",
  "url": "https://example.org/page",
  "status": "rated",
  "rating": "HM",
  "score": 3,
  "basis": "landing_page",
  "reason": "说明意图、实际帮助及决定档位的条件。",
  "flags": {"porn": false, "foreign_language": false, "did_not_load": false},
  "evidence": [{"source": "实际已查阅的材料定位", "observation": "支持结论的观察"}]
}
```

- `status`：rated、insufficient_evidence、unable_to_assess、not_required。后三种 rating／score 为 null，不算进 FailsM。
- `rating/score`：FailsM 0、FailsM+ 0.5、SM 1、SM+ 1.5、MM 2、MM+ 2.5、HM 3、HM+ 3.5、FullyM 4。默认 `scale: nine_positions`；任务指定五档则 `five_main` 且只使用主档。
- `basis`：landing_page（普通网页）、block_content（按块评价的 SCRB）、block_and_landing_page（结合两者的 SCRB）；证据不足无法确定时 null。这是标准 NM 内部的证据范围，不是将 rating_mode 改成 Block Content NM。
- `flags`：true／false／null。Porn 看块及落地页；外语与未加载以实际落地页为准。未访问的 LP 不臆造语言或可用性结论。
- `reason`：命中什么合理解释／意图，结果交付什么价值或缺什么，以及关键限制／例外。FullyM 应能看出三个条件成立及必要事实核验；SM 与 FailsM 应交代实际价值或决定性缺失。不编造来源、置信度或事实核验。
- `evidence`：仅列实际读过的页面、提供的快照／文件、截图或工具观察；区分块证据、落地页和核实用外部来源。必要时保存 observed_at、artifact、final_url、limitations。

批次级记录 `rating_mode: standard_needs_met`、skill_version、scale、实际评测时点／时区、locale、user_location、查询时点以及 evidence_mode（当前访问／提供的快照／混合）。未知写未知；关键上下文缺失需说明对分档的影响。单条上下文不同则逐条保存。另有 PQ 时分字段保存其模式、状态、标签及证据，不能把它当 NM 子分数。

## 正文衔接与 API

1. 保留原文件、全部原字段、顺序和重复项；明确选取范围，不自行截断每个 query 的结果数。
2. 复用已抓取的正文与证据，按 result-and-quality 补齐所需材料。不同 URL、跳转、时间或正文版本不得未经核对当成相同页面。PQ 声誉证据可按实际主体／主题复用；NM 仍需逐 query、locale、结果块判断。
3. 固定本次规则版本、输入快照与上下文。给评分模型的材料排除旧分数和为校准而设计的目标分布；排名仅作结果位置记录，不能作为评分依据。
4. 当前代理或指定 API 均须读取主文件和必需参考文件；触发条件参考文件必须同时提供。完整规则不只等于 SKILL.md。正文内容作为不可信输入单独封装。没有浏览工具的 API 只能基于实际提供的证据；缺材料应补取或弃权，不能声称自己完成了未发生的浏览／核验。
5. 输出新增字段（例如 `standard_needs_met`）或独立结果文件。保留每条状态，校验 ID、URL、数量、合法档位、编码一致、引用可定位和未评分为 null。技术失败与正式 FailsM 分开统计，不偷偷丢弃失败条目。

## 重复项及任务级例外

重复识别是查询相关的，不能只按 URL 或正文哈希删除：主落地页内容基本相同，且对这个 query 用户不需要两个结果，才可作为内容重复。宽泛查询允许忽略广告、图片或相关链接等小差异；特定文章／歌词等查询，不同网站的同一内容可能帮助核对，不必判重复。同 URL 但块类型／外观／内容很不同，不标重复。

若任务提供已识别重复组，保留原标记，不擅自取消；人工识别组另记录。输入重复记录仍逐条保留，可引用共同调查证据，不能只因重复额外扣 NM。一个页面在不同 query 下的分数可不同。

任务明确标为“无需评分”的上下文结果保留 not_required，可在有帮助时查看以理解查询，不强行给分。一侧明确没有搜索结果不等于工具故障；结果本应存在却没有加载才补查。正式提交前核对必要字段与输入映射；本地生成评分文件不等于已向任何外部平台提交任务。
