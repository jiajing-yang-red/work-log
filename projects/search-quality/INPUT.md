# 搜索质量评估 Pipeline

输入模块 `quality_input` 将搜索结果转换为可供评测的数据包。
Python 3.9+，仅使用标准库，无需安装依赖。

第一层：`quality_input`，本页介绍输入与校验。
第二层：`quality_eval` + `quality_llm`，见 [评估方案与模型实验说明](EVALUATION.md)。
排序指标由 `quality_metrics` 计算，可在本地界面查看。

本地前端：在项目根目录运行 `python3 -m frontend`，然后打开
`http://127.0.0.1:8765`，即可点击启动评测、查看进度和 URL 评分结果。
服务自动读取 `.env` 中的 API Key，详细说明见 [前端使用说明](frontend/README.md)。

输入层读取、冻结、统一和校验搜索数据，交付下一层需要的原始证据。
它不调用搜索 API，不抓取网页，不调用评分模型，不计算 Precision 或 Recall。

## 运行

从仓库根目录进入本项目：

```bash
cd projects/search-quality
```

导入自己的 Ateve SQLite 数据库：

```bash
python3 -m quality_input import \
  --input /path/to/search-sample.sqlite \
  --output ../output/search-quality-input-new-run \
  --dataset-id ateve-sample \
  --use-collection-time
```

输出目录必须不存在；不会覆盖已完成或失败的导入。
退出码 `0` 表示结构检查通过，`2` 表示参数错误、导入失败或存在阻断问题。
`ready` 仅表示结构可供下游消费，不代表所有结果均能打分或搜索质量良好。

核验已有数据包：

```bash
python3 -m quality_input inspect ../output/search-quality-input-new-run
```

运行合成示例：

```bash
python3 -m quality_input import \
  --input examples/input.json \
  --output /tmp/quality-input-json-demo \
  --use-collection-time

python3 -m quality_input import \
  --input examples/flat.csv \
  --mapping examples/fields.json \
  --output /tmp/quality-input-csv-demo \
  --evaluation-as-of 2026-10-20T13:36:00+08:00
```

示例域名和内容为虚构数据。重复执行请换一个输出目录。

## 支持的格式

| 格式 | 契约 |
| --- | --- |
| SQLite | 专用 `ateve_sampling_sqlite_v1` 适配器，要求 `sampling_run`、`queries`、`query_urls` 以及 Ateve 来源标识 |
| JSON | 对象，独立包含 `queries`、`search_runs`、`responses` 三个数组；可追加 `page_snapshots`、`reference_labels` |
| JSONL | 每行一个扁平记录对象，一行一条结果；无结果的 query 必须提供状态行 |
| CSV | 与 JSONL 同样的扁平契约；支持 UTF-8 BOM；一行一条结果或状态行 |

`--format auto` 根据扩展名选择**已声明的适配器**，随后检查内容。
不猜测供应商字段含义，不把任意 SQLite 当成已支持的数据库。
新的原生供应商格式需要增加适配器；例如原生 Exa JSON 尚无专用适配器，
但可以通过标准 JSON 或配置字段映射的扁平格式接入。

### JSON 的最小完整结构

```json
{
  "queries": [
    {"query_id": "q1", "query_text": "ExampleDB installation"},
    {"query_id": "q2", "query_text": "ExampleDB latest release"}
  ],
  "search_runs": [
    {
      "search_run_id": "run-a",
      "provider": "demo",
      "requested_top_k": 10,
      "query_scope": ["q1", "q2"]
    }
  ],
  "responses": [
    {
      "query_id": "q1",
      "search_run_id": "run-a",
      "status": "ok",
      "results": [
        {
          "rank": 1,
          "url": "https://example.com/install",
          "title": "Installation",
          "snippet": "Synthetic installation guide."
        }
      ]
    },
    {
      "query_id": "q2",
      "search_run_id": "run-a",
      "status": "empty",
      "results": []
    }
  ]
}
```

SearchRun 的 `query_scope` 缺省为完整 Query 清单。缺少计划内的响应时，生成
`missing_record` 响应并报告，保留评估范围。明确的子集可以用 `query_scope` 指定。
Query 不根据文本自动合并；同一文本的不同时间、地区或上下文可以使用不同 ID。

### 扁平文件与字段映射

每行须能得到 `query_id`、`query_text`、`search_run_id`、`provider`。
返回结果行须提供 `rank`。无结果状态行的所有结果字段须为空或缺省，
并明确提供 `status=empty/error/not_collected` 等状态。

支持的标准字段：

- Query：`query_id, query_text, raw_query, language, locale, category, query_issued_at, request_frequency`
- SearchRun：`search_run_id, provider, system_version, requested_top_k, collection_started_at, collection_completed_at`
- SearchResponse：`status, http_status, attempt_count, retrieved_at, reported_result_count, error_kind, error_message`
- SearchResult：`rank, url, title, snippet, text, published_at, source_score`

映射格式见 `examples/fields.json`：

```json
{
  "fields": {
    "query_text": "query",
    "rank": "position",
    "snippet": "description",
    "source_score": "score"
  },
  "defaults": {
    "search_run_id": "experiment-1",
    "provider": "demo",
    "requested_top_k": 10
  }
```

`fields` 的键是标准字段，值是来源中的**直接键名**，不执行表达式或脚本。
未映射字段按同名字段读取。映射中的来源键必须在文件中至少出现一次；
可选键在个别记录中缺失时保留空值。
`defaults` 只允许 SearchRun 字段，且仅在字段缺省时使用。
重复行上的同一 query、run 或 response 公共字段如果存在冲突，阻止正式使用。

扁平文件只能知道它明确列出的 query。若某条 query 完全未写入文件，本层无法凭空恢复它。
需要独立、完整评估范围时使用 JSON 对象格式或 SQLite。
多供应商对比推荐 JSON，明确提供各 SearchRun 的 `query_scope`。

## 标准化输出契约

```text
dataset/
├── manifest.json
├── queries.jsonl
├── responses.jsonl
├── input_report.json
├── raw/source.sqlite       # 或 source.json / source.jsonl / source.csv
├── page_snapshots.jsonl    # 有导入记录时生成
└── reference_labels.jsonl  # 有导入记录时生成
```

### manifest.json

包含格式版本、实现版本、数据集 ID/版本、导入时间、原始路径和快照哈希、
适配器版本、映射配置、时间口径、完整 query_scope、SearchRun 清单、文件校验和。
数据集版本由冻结来源、映射、时间策略及实现版本确定，输出位置和导入时钟不影响版本。

所有 Query、SearchRun、Response ID 的作用域都是该数据集。跨数据集比对时必须同时使用
数据集版本和 ID；同一评估集的多供应商结果应共享 Query 清单。

SearchRun 保存：

`search_run_id, provider, system_version, collector_version, requested_top_k,
collection_started_at, collection_completed_at, request_parameters,
query_scope, source_query_window, source_ref`

采集工具版本 `collector_version` 不冒充搜索系统版本 `system_version`。
Ateve 适配器只提取已知检索参数 `top_k`；其他原始配置留在原始文件，
不会将连接、身份信息一并传给评分器。

### queries.jsonl

每行一个 Query：

`query_id, query_text, raw_query, language, locale, category, query_issued_at,
query_issued_at_raw, request_frequency, source_ref`

查询文本不截断、不改写。未知上下文保留 `null`。不猜测语言、意图和类别，
也不自动将 `request_frequency` 当作指标权重。

### responses.jsonl

每行一个 `(search_run_id, query_id)` 响应，包含：

`response_id, query_id, search_run_id, status, source_status, http_status,
attempt_count, retrieved_at, retrieved_at_raw, reported_result_count,
actual_result_count, error, source_ref, results`

标准状态：

| 状态 | 含义 |
| --- | --- |
| `ok` | 来源明确成功且实际结果非空 |
| `empty` | 来源明确成功且实际结果为空 |
| `error` | 来源明确执行失败 |
| `not_collected` | 来源明确未采集；`pending/skipped` 也映射到此状态 |
| `missing_record` | 计划执行的 query 没有对应响应 |
| `unknown` | 无法从来源确定状态，不能因为 HTTP 200 或结果为空就推断成功 |

每条 SearchResult 包含：

`result_id, rank, document_key, url, title, snippet, text, published_at,
published_at_raw, source_score, source_metadata, raw_ref, evidence_hash,
page_snapshot_id`

- `result_id`：由 response ID 与原始 rank 生成。一个 URL 占两个位置就保留两个结果。
- `document_key`：`url_exact_v1:` 加原始 URL 字符串的规范 JSON SHA256；URL 不删参数、不重定向、不语义去重。URL 缺失时为 `null`。
- `evidence_hash`：内容证据哈希，排除 rank、source_score 及供应商列表序号 `source_metadata.id`。
- `source_score`：API 自身分数，不是质量评分。未评分也不自动填 0。
- `raw_ref/source_ref`：SQLite 表及主键、JSON Pointer 或扁平记录位置。
- `snippet/text`：分别保留完整内容；没有独立正文时不伪造 `text`。

页面正文和参考标签不内嵌到 API 证据，避免不同评估模式混淆。

### 可选网页快照

JSON 输入可附加 `page_snapshots`，结果通过 `page_snapshot_id` 引用：

`page_snapshot_id, requested_url, final_url, fetched_at, fetch_status, http_status,
content_type, body_text, raw_content_ref, extractor_version, extraction_status,
truncated`

`fetch_status` 支持 `ok/error/not_fetched/unknown/null`；未知不自动填成功。
快照要求提供 `requested_url`；结果关联到快照时，URL 必须与请求地址或最终地址匹配。
对正文生成 `content_hash`。若输入给出哈希，必须采用本项目规范 JSON 字符串的 SHA256
并与正文匹配。文本原样保留，`truncated` 的未知状态也不自动填 `false`。
`raw_content_ref` 仅作为来源记录保存，本层不跟随路径读取或发起网络请求。

这部分验证引用、字段及正文哈希，不证明抓取完整、内容可信或足以评分。
缺少正文时产生问题记录，不回退为用 snippet 冒充正文。

### 可选参考标签

JSON 输入可附加 `reference_labels`。核心字段：

`query_id, document_key, grade, label_schema_version, source, version,
evidence_mode, reference_scope, query_spec_version`

- `grade` 可为有限数值或 `null`，保留标签体系本身的范围，输入层不解释等级。
- `evidence_mode=api_only` 必须引用同一 query/document 的具体 `result_id`。
- `evidence_mode=page_content` 必须引用已导入的 `page_snapshot_id`。
- `evidence_mode=document` 允许独立文档参考标签；文档不必出现在当前返回列表中。
- `reference_scope` 必须是 `pooled/corpus_complete/unknown`，仅记录来源声明。
- 同一 query/document/证据/标签版本不能重复绑定。

API 结果 ID 是标准化层生成的；导入已有 API 标签时，来源应提供相同 run/query/rank
对应的稳定结果 ID。不要用 URL 级标注冒充对另一条 API 摘要的判断。

本层不核实参考集合是否穷尽整个语料库，因此报告始终注明
`reference_completeness_verified=false`。仅有本次 Top K 的评分不能得到全库 Recall。
候选池参考集也必须保留其范围，不能自动宣称全库召回率。

## 时间规则

保留三种不同的时间：

1. query 来源窗口 / `query_issued_at`；
2. 结果采集时间 `retrieved_at`；
3. 评估时间基准 `evaluation_as_of`。

默认不替用户决定“最新”相对于哪一天，产生 `EVALUATION_TIME_UNSPECIFIED` 警告。
可选 `--evaluation-as-of` 明确指定带时区的时间，
或者 `--use-collection-time` 选用所有运行完成/响应采集时间中的最晚时间。
后者是重新采集评估口径，不是历史重放；多个供应商跨时段采集的可比性需后续控制。

时间统一为 UTC，保留可用的原始时间值；纳秒来源转换到微秒精度。
仅日期或无时区字符串不猜测为某地零点，标准时间设为 `null` 并报告。
未知发布时间不等于过期，采集时间不填入发布时间。

## 校验、留档与失败行为

- SQLite 用只读连接及 SQLite backup 生成一致性快照，包含已提交 WAL 数据。
  冻结副本转换为单文件 journal 模式，原数据库不变。
- CSV/JSON/JSONL 留档原始字节；来源和标准化文件都有校验和。
- 冲突字段、非法/重复/不连续 rank、关联错误、数量不一致、非法 JSON 等为阻断问题。
- 缺标题/URL/文本、缺日期、未知状态等保留结果并给警告；不直接赋质量分。
- 精确重复 URL、少于 Top K 等是保留事实，不清洗掉。
- 阻断后仍保留原始文件和 `input_report.json`。可能生成诊断性的不完整标准数据，
  但其 manifest 为 `blocked`，公共读取接口拒绝消费。
- 不执行 query、snippet、网页内容或映射中的指令。
- manifest 及文件哈希用于发现意外改动，不是签名或对抗恶意篡改的认证机制。

输入报告包含 Query/Response/Result 数量、各状态分布、缺失字段统计、
重复/短返回情况、网页和标签数量、可用证据统计，以及定位到原始记录的问题列表。

## 对第二层的 Python 接口

```python
from quality_input.pipeline import load_dataset, iter_api_inputs

dataset = load_dataset("../output/search-quality-input-new-run")

# query 级统计范围始终来自完整清单及各 SearchRun 的 scope。
queries = dataset["queries"]
responses = dataset["responses"]

for envelope in iter_api_inputs(dataset):
    result_id = envelope["result_id"]
    collection_status = envelope["collection_status"]
    judge_input = envelope["judge_input"]
    # 第二层：根据 query 生成/读取固定的 query_spec，再组合评分 prompt。
    # 保存评分时绑定 dataset_version、result_id、evidence_hash 和评分配置。
```

`judge_input` 包含 query、独立上下文、评估时间、API URL/标题/完整文本/日期，
以及 display_url/images 元数据。默认不暴露 provider、rank、source_score 或供应商列表序号。
页面缺失不在此处生成页面评分请求。

`iter_api_inputs` 一条实际结果产生一个 envelope；它不负责 query 级分母。
无结果、失败、缺记录的 query 必须从完整 `queries/responses` 进入下游汇总。
失败响应若带有部分结果，也保留 envelope 的执行状态，不能悄悄当作完整成功响应。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖：空结果/失败/缺记录区分、重复位置和长文本保留、结构冲突阻断、
多供应商关联、字段映射、参考池中的未召回文档、页面独立证据、
时间精度、冻结版本及改动检测、WAL 快照和源文件保护。

当前输入层面向离线批量输入，在内存中处理整批记录。海量数据的流式导入、
网页采集、原生 Exa 适配器不在输入层版本中。评分调度见第二层文档，指标计算尚未实现。
