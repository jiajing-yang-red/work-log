# 搜索结果质量评测

导入搜索结果后，可以按选定规则对结果评分、检查证据和失败原因，并在本地界面查看排序效果。最近一次批量评测覆盖 4,000 个查询。[实验进展](PROGRESS.md)

## 运行离线示例

从仓库根目录执行以下命令。示例使用合成数据和模拟模型，Python 3.9 及以上即可运行，无需 API Key。

```bash
cd projects/search-quality
python3 -m quality_input import --input examples/input.json --output ../output/demo-input --use-collection-time
python3 -m quality_eval prepare --dataset ../output/demo-input --config examples/evaluation.mock.json --output ../output/demo-specs
python3 -m quality_eval run --dataset ../output/demo-input --config examples/evaluation.mock.json --specs ../output/demo-specs --output ../output/demo-ratings
```

运行摘要位于 `../output/demo-ratings/summary.json`。再次运行请使用新的输出目录。要在 macOS 或 Linux 上使用本地工作台，在当前目录运行 `python3 -m frontend`，再打开 `http://127.0.0.1:8765`。真实模型的配置变量见 [.env.example](.env.example)。

## 代码结构

| 路径 | 职责 |
| --- | --- |
| [quality_input/](quality_input/) | 导入、规范化和校验数据，生成可重复使用的数据包。[输入格式](INPUT.md) |
| [quality_eval/](quality_eval/) | 组织评分方案和提示词，执行实验、校验证据、记录任务和重试。[实验配置](EVALUATION.md) |
| [quality_llm/](quality_llm/) | 适配模型协议，处理流式响应、超时与限流。 |
| [quality_metrics/](quality_metrics/) | 计算 NDCG、MRR 和 Success，并按共同查询对比检索配置。 |
| [frontend/](frontend/) | 本地工作台，查看运行进度、评分和逐查询结果。[使用说明](frontend/README.md) |
| [examples/](examples/) · [tests/](tests/) | 合成输入、模型配置样例和回归测试。 |

结果块评分器随项目保存了一份固定版本的规则，供历史实验复现。独立使用的 skill 收录在仓库的 [skills 目录](../../skills/)。

## 测试

```bash
python3 -m unittest discover -s tests
node tests/test_frontend_timing.cjs
```

第二条单独检查前端计时显示，需要 Node.js。
