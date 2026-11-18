# 执行与恢复

## 环境

使用 Python 3.10+。在任务目录建立虚拟环境，不把密钥或虚拟环境装进 skill：

```sh
FETCH_SKILL="${CODEX_HOME:-$HOME/.codex}/skills/url-body-fetcher"
python3 -m venv .venv-fetch
.venv-fetch/bin/python -m pip install -r "$FETCH_SKILL/scripts/requirements.txt"
.venv-fetch/bin/python "$FETCH_SKILL/scripts/fetch.py" --help
```

先核对范围，再请求。以下每条命令是一个独立步骤：

```sh
.venv-fetch/bin/python "$FETCH_SKILL/scripts/fetch.py" --url 'https://example.org/article' --output outputs/body-fetch --dry-run
.venv-fetch/bin/python "$FETCH_SKILL/scripts/fetch.py" --input results.json --output outputs/body-fetch --provider direct
.venv-fetch/bin/python "$FETCH_SKILL/scripts/fetch.py" --input results.json --output outputs/body-fetch --provider deepseek --env-file .env
.venv-fetch/bin/python "$FETCH_SKILL/scripts/fetch.py" --input results.json --output outputs/body-fetch --provider brightdata --env-file .env
.venv-fetch/bin/python "$FETCH_SKILL/scripts/fetch.py" --input results.json --output outputs/body-fetch --provider platform --env-file .env
```

`--provider` 还可选 `firecrawl`、`jina`、`import`、`export`。每次沿用同一输入和输出目录，后续通道只处理尚无可用主体文字的记录。同一通道已尝试记录默认不重复请求；有充分理由重试时加 `--retry-failed`。

## 输入

接受 JSON 对象、对象数组或 JSONL（每行一个对象），默认 URL 字段为 `url`。也可多次传 `--url`。`--rows-key` 和 `--results-key` 支持点分隔的对象路径；`--url-key` 是结果对象中的字段名。

例如 `{"queries":[{"query":"问题","results":[{"url":"https://example.org/a","rank":1}]}]}`：

```sh
.venv-fetch/bin/python "$FETCH_SKILL/scripts/fetch.py" --input search.json --rows-key queries --results-key results --top-k 10 --output outputs/body-fetch --provider direct
```

Top K 按每个数组的原始位置取前 K 条，不重排 rank。不足 K 条时保留实际条数。未选中的结果仍原样保留在导出中。CSV 或更复杂结构需先显式转换并保留原始行映射。

输出字段默认 `page_content_fetch`，可用 `--field` 换名；若输入已存在同名字段会拒绝覆盖。输入内容或选择规则变化后不能复用旧输出目录，应建立新目录。

## 调度与恢复

- 默认并发 8、全局 QPS 4、同域间隔 0.5 秒；它们是本脚本保守默认值，不是厂商配额承诺。分别用 `--workers`、`--qps`、`--domain-interval` 调整。
- 默认超时 60 秒、重试 2 次。429 的服务端等待时间优先；较长等待或配额问题保存 pause 文件并结束当前通道，不长期占住交互。
- Bright Data 默认每批 50 条、最多 4 个未完成任务、一次轮询预算 45 秒；网络请求自身可能超过轮询预算。用 `--batch-size`、`--max-new-jobs`、`--poll-budget`、`--poll-interval` 调整。`--max-new-jobs` 实际限制未完成任务总数。
- 再运行同一 Bright Data 命令，会先查询原 snapshot，再提交符合条件的新批次。直到 summary 的 pending jobs 和当前通道 eligible 数均为 0，或出现明确阻碍。
- `submission_unknown` / `submission_started` 保留为待核实任务。通过有权限的控制台查找原 snapshot，核实后恢复原任务；不要删除任务文件并盲目重提。
- 默认使用环境中的代理；可用 `--proxy` 指定或 `--no-proxy` 禁用。不硬编码代理端口。
- 默认拒绝本地和私有地址，直接请求还检查 DNS 和跳转目标。只有用户明确要求读取内部地址时才使用 `--allow-private`。
- 默认每次直接下载上限 20 MiB，可用 `--max-bytes` 调整；截断内容标为 partial。大型 PDF 或扫描 PDF 应单独处理。

## 导入已有正文

浏览器、官方 API 或已有证据取得的文字可导入；不要重新请求。导入清单为 JSON 数组，文件相对路径以清单所在目录为基准：

```json
[{"url":"https://example.org/article","provider":"official_content_api","text_file":"article.txt","raw_file":"response.json","title":"文章标题","source_url":"https://example.org/article","retrieval_scope":"official_content_api"}]
```

```sh
.venv-fetch/bin/python "$FETCH_SKILL/scripts/fetch.py" --input results.json --output outputs/body-fetch --provider import --import-file imported.json
```

`url` 必须属于所选输入，`source_url` 写实际来源。公开文档副本使用 `retrieval_scope: document_copy_not_original_url`，初始标记 partial，核对作品、版本、日期后再人工判断。仍应保留真实来源，不能改写成原 URL 直抓。

## 人工上下文校正

记录实际依据后，针对具体 attempt ID 校正；不要凭长度或错误关键词批量改为成功。

```json
[{"url":"https://example.org/article","attempt_id":"direct-1","status":"content_obtained","reason":"verified_target_body","evidence_note":"已核对标题、日期与正文；404 是文章讨论主题，不是错误页。"}]
```

```sh
.venv-fetch/bin/python "$FETCH_SKILL/scripts/fetch.py" --input results.json --output outputs/body-fetch --provider export --review-file reviews.json
```

export 不请求网络。校正保留原判定和检查说明；正向校正要求已有文字文件。汇总始终从各次尝试中选择当前最可用的结果，不会删除其他证据。

## 产物

`enriched.json` 保留原数据并添加抓取字段；`results.jsonl` 为去重 URL 结果；`summary.json` / `report.html` 为汇总；`needs_followup.json` 列出待继续处理项。`records/` 保存全部尝试，`content/` 保存提取文字，`raw/` 保存响应，`jobs/` 保存异步状态，`schemas/` 缓存抓取器 schema。unresolved 的导出正文为空，但原始证据仍保留。不要将含未脱敏网页数据的产物擅自对外发布。
