# 抓取通道与接口维护

以下接口于 2026-11-07 对照官方文档整理。厂商可能更新 schema、配额或访问范围；报接口不兼容时先核对官方文档，不凭旧价格或旧限制推测账户能力。

| 通道 | 接口与配置 | 结果解释 |
|---|---|---|
| direct | GET 目标 URL，无厂商密钥 | 保存字节、目标 HTTP 状态、最终 URL，再提取 HTML/PDF 主体。 |
| DeepSeek | POST `https://api.deepseek.com/v1/tools/fetch`；Bearer `DEEPSEEK_API_KEY`；`{"url":"…"}` | 读取 title、content。API 404 表示未取得可用内容，可能涉及目标错误、策略限制、类型不支持或无可提取内容，不等于源站 404。 |
| Bright Data Fetch | `BRIGHT_DATA_API_KEY`；dataset `gd_m6gjtfmeh43we6cqc` | 读取 markdown、html2text 或 page_html，保留 page_title、final_url 和 input.url。批量使用异步任务。 |
| Bright Data platform | 同一密钥，按 URL 类型选择 dataset | 逐条检查 error；帖子主体、评论、字幕与元数据分开解释。外层 HTTP 200 不代表每条成功。 |
| Firecrawl | POST `https://api.firecrawl.dev/v2/scrape`；Bearer `FIRECRAWL_API_KEY` | 请求 markdown、onlyMainContent；读取 data.markdown 和 metadata，区分 API 状态及目标 statusCode。 |
| Jina Reader | GET `https://r.jina.ai/` + 完整目标 URL；可选 `JINA_API_KEY` | 先检查 Warning 再取 Markdown Content；URL Source 不应当作已验证的最终跳转地址。无 Key 使用更低请求频率。 |

官方参考：[DeepSeek Fetch](https://api-docs.deepseek.com/api/fetch/)、[DeepSeek Search/Fetch 错误说明](https://api-docs.deepseek.com/guides/search/)、[Bright Data Fetch 请求](https://docs.brightdata.com/products/scrapers/fetch-api/send-first-request)、[Firecrawl Scrape](https://docs.firecrawl.dev/api-reference/endpoint/scrape)、[Jina Reader](https://jina.ai/reader/)。

## Bright Data 异步协议

通过 `POST https://api.brightdata.com/datasets/v3/trigger` 提交，查询参数包含 dataset_id、include_errors=true，body 为 URL 对象数组。保存返回 snapshot_id，通过 `GET /datasets/v3/progress/{snapshot_id}` 查询，ready 后通过 `GET /datasets/v3/snapshot/{snapshot_id}?format=json` 下载。

保存完整批次响应，按每个 item 的 input.url 或 url 精确关联原 URL，不按数组位置匹配；不匹配的结果保留原响应，不能贴到另一条 URL。异步提交请求不能自动重复重试。超时后先确认是否已创建任务。

平台 schema 地址为 `https://docs.brightdata.com/scrapers/{dataset_id}.json`；脚本核验 collect_by_url 必填字段仅有 url，再提交。

| URL 类型 | dataset ID |
|---|---|
| Reddit 帖子 | `gd_lvz8ah06191smkebj4` |
| YouTube 视频 | `gd_lk56epmy2i5g7lzu0k` |
| YouTube 频道 | `gd_lk538t2k2p1k3oos71` |
| Facebook 帖子 | `gd_lyclm1571iy3mv57zw` |
| Instagram 帖子 | `gd_lk5ns7kz21pck8jpis` |
| Instagram 账号 | `gd_l1vikfch901nx3by4` |
| LinkedIn 公司 | `gd_l1vikfnt1wgvvqz95w` |
| LinkedIn 帖子 | `gd_lyy3tktm25m4avu764` |
| X 帖子 | `gd_lwxkxvnf1cynvib9co` |
| Quora 主站问答 | `gd_lvz1rbj81afv3m6n5y` |

子域、短链接、reel、个人档案等不要机械套用另一类抓取器。必要时查官方 schema 或使用其他通道。

## 官方内容接口、浏览器和文档副本

有官方内容 API 时，使用页面 ID、语言、版本和日期核对身份，处理分页和服务要求的 backoff；接口中的摘要或元数据不能当全文。优先使用可用的服务连接器。读取公开仓库时核对文件路径和 revision。

浏览器适合 JavaScript 渲染、公开可见正文的补抓。只保存实际看到/读取的内容，记录是否展开全文、是否加载评论，以及仍不可见的部分。需要登录或权限时使用用户授权流程。

公开文档镜像或作者版本必须核对标题、作者、版本及页数。实际 source_url 与请求 URL 分别保留，不能把类似主题文章当成替代正文。通过 import 通道合并已有结果，格式见 execution.md。
