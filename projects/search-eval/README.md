# 搜索离线自动评测

读取离线搜索结果与相关性标注，计算 Precision、Recall、F1、MAP 和 NDCG。提供多个 K 值、逐查询和分类汇总、标注覆盖率、基线对比及可直接打开的 HTML 报告。

Python 3.9+，仅使用标准库，无需安装依赖。当前版本是命令行工具与静态报告，不包含搜索服务、标注后台或定时任务。示例中的数据和分数均为演示数据。

## 快速运行

在项目目录运行：

```bash
python3 -m search_eval \
  --qrels examples/qrels.csv \
  --results examples/results_baseline.csv \
  --ks 3 5 10 \
  --name baseline-v1 \
  --output reports/baseline

python3 -m search_eval \
  --qrels examples/qrels.csv \
  --results examples/results_candidate.csv \
  --ks 3 5 10 \
  --name candidate-v2 \
  --baseline reports/baseline/report.json \
  --output reports/candidate
```

打开 `reports/candidate/report.html` 查看结果。每个输出目录包含：

| 文件 | 用途 |
| --- | --- |
| report.html | 整体指标、分类汇总、查询筛选、排名与标注明细、基线退步查询 |
| report.json | 原始精度的指标、逐查询详情、配置、输入文件哈希、全部版本差值 |
| per_query.csv | 逐查询指标，可导入表格工具 |
| summary.csv | 整体指标与基线差值 |

输出目录必须不存在或为空，避免覆盖历史评测。每次运行使用新的目录。运行时间写为带时区的 UTC ISO 时间。

## 输入规范

两类输入都支持 UTF-8 CSV（含 BOM）、JSON 对象数组或 JSONL。Excel 文件请先另存为 UTF-8 CSV。文档 ID 必须是跨搜索版本稳定的标识符，不能用易变的标题或本次结果序号代替。

### 相关性标注 qrels

| 字段 | 必需 | 说明 |
| --- | --- | --- |
| query_id | 是 | 查询唯一标识；实际评估范围由此文件中的查询集合决定 |
| doc_id | 是 | 文档唯一标识 |
| grade | 是 | 非负相关性等级，也接受字段名 relevance；支持 0–30 的有限数值 |
| query | 否 | 查询文本；同一 query_id 必须一致 |
| category | 否 | 查询分类；同一 query_id 必须一致；缺省为未分类 |

```csv
query_id,query,category,doc_id,grade
q1,搜索评价指标,关键词查询,d1,3
q1,搜索评价指标,关键词查询,d2,2
q1,搜索评价指标,关键词查询,d3,0
```

推荐四级标注：0 不相关，1 弱相关，2 相关，3 高度相关。默认二值相关阈值为 2。如果已有 0/1 标注，运行时加 `--threshold 1`。等级阈值只影响 Precision/Recall/F1/MAP；NDCG 始终使用原始等级及所选增益函数。

同一查询与文档组合只能出现一次，冲突或重复都报错。查询没有相关答案时，可以标注至少一个判断为 0 的候选文档，以保留该查询；当前格式不支持完全没有任何文档标注的查询。

### 搜索结果 results

```csv
query_id,doc_id,rank
q1,d2,1
q1,d1,2
q1,d3,3
```

必需字段为 query_id、doc_id、rank。rank 从 1 开始，同一查询必须连续、唯一；同一查询不能返回重复 doc_id。文件记录可以无序，计算前按 rank 排序。额外字段（如 score）暂不参与计算，显式 rank 决定次序，包括同分结果。

标注中存在但结果文件中没有的查询，作为空结果保留，不能通过漏掉失败查询来抬高均分。离线文件本身无法区分执行失败与真实空结果，报告会提示检查采集日志。结果中出现完全没有标注的 query_id 会报错；已知查询的未标注文档按 0 分处理，并计入未标注数量。

## 指标口径

令 R 为该查询在整个标注集中达到相关阈值的文档数，rel(i) 表示第 i 名是否达到阈值，H@K 为前 K 名相关文档数。

| 指标 | 定义 |
| --- | --- |
| Precision@K | H@K / K；返回不足 K 条时，缺失位置视为不相关 |
| Recall@K | H@K / R |
| F1@K | 2 × Precision@K × Recall@K / (Precision@K + Recall@K) |
| AP@K | Σ(i=1…K) Precision@i × rel(i) / R |
| MAP@K | 所有测试查询的 AP@K 等权平均 |
| MAP | 对每个查询输入文件中的完整排名计算 AP，再等权平均；完整排名指提交的结果范围，不声称覆盖整个语料库 |
| NDCG@K | DCG@K / IDCG@K，DCG = Σ gain(grade_i) / log₂(i+1)；IDCG 根据该查询的全部标注文档降序计算前 K 名 |

默认 NDCG 增益 `gain(g) = 2^g − 1`；可用 `--gain linear` 改成 g。NDCG 的等级增益与二值相关阈值互相独立，例如只有等级 1 的文档时，阈值为 2 的 Recall 为 0，但 NDCG 仍可大于 0。

R=0 时 Recall、AP、F1 为 0；IDCG=0 时 NDCG 为 0。所有查询始终参与宏平均，并在报告中提示无二值相关文档的查询数。整体 F1 是逐查询 F1 的均值，不是整体平均 Precision 与 Recall 的调和平均。无答案查询混在均值中会降低这几项指标，建议利用 category 单独查看其分组。

标注覆盖率 = 已标注的实际返回文档数 / 实际返回文档数，按 K 汇总。没有返回文档时为 null，不记成 100%。该值衡量返回结果被判断的充分程度，不代表语料库中所有相关文档已经找全。

## 版本对比与可复现性

使用 `--baseline` 指向相同评测器生成的 report.json。只有评测器版本、计算配置、标注文件 SHA256 及查询集合均一致时才允许比较。输入文件的任何字节变化都会改变哈希；即使只调整顺序，也需要重跑基线。

JSON 中保存全部指标与查询差值。HTML 按最小 K 的 NDCG 展示退步查询，差值为当前分数减基线分数，采用 0–1 原始分值。当前不做显著性检验或自动发布判定。

`--name` 可填写搜索版本、索引快照和实验标识，例如 `bm25-v2-index20261016`。要实现搜索实验的完整复现，仍需在上游保留语料快照、检索参数及索引构建版本；输入哈希只能确认本次离线文件一致。

## 测试与后续接入

```bash
python3 -m unittest discover -v
```

测试覆盖手工计算结果、截断与短返回、理想排序分母、宏平均、二值标注、空结果、未标注、非法排名、重复 ID、输入格式及基线兼容性。

后续接入真实文件只需转换为上述两类记录。若需要定时评测，可由现有 CI 或任务调度器执行命令并归档输出；当前工具不会自行调用网络接口或启动调度任务。

## 设计参考

数据拆分参考 TREC 的 qrels 与 run 思路，本工具使用带表头的行记录，未直接实现 TREC 空格分隔文件解析。AP@K 的分母采用全部已标注相关文档数，与 trec_eval 的 map_cut 一致。NDCG 默认指数增益，而 trec_eval 的 ndcg_cut 默认线性等级增益，因此默认分数不能直接混用；如需对齐，应统一阈值、增益、查询集合、排序与零相关查询处理方式。

- [NIST trec_eval](https://github.com/usnistgov/trec_eval)
- [Precision at cutoffs](https://github.com/usnistgov/trec_eval/blob/main/m_P.c)
- [MAP at cutoffs](https://github.com/usnistgov/trec_eval/blob/main/m_map_cut.c)
- [NDCG at cutoffs](https://github.com/usnistgov/trec_eval/blob/main/m_ndcg_cut.c)
