# PQ 1.2.0：本次完整对照复核

2026-11-07，针对用户提供的 September 11, 2025、182 页版本重新核对 Part 1 全部 75 个目录条目及其例子，并复核联合 Needs Met、外语／未加载和附录任务处理规则。原 PDF SHA-256 为 `46aa9ea1fd004a09d68ececd7acdb18ec505061f52598fa69a856dfea94a6a06`。以下旧维护记录仅保留审计来历，不进入评分提示词。

五个主档的英文开头定义逐段匹配原文（只消除 PDF 排版空格），中文定义、初始考虑、判断条件及例外保留。尤其复核：High／Highest 的“至少一项”与其基础条件同时适用；Medium 混合质量不是仅允许轻微瑕疵；必要 E-E-A-T 缺失与 Lowest 不能被其他优点抵消；小站无声誉、论坛新帖、正常付费墙、定制错误、PDF／工具／媒体等边界均保留。

本次补足的操作边界：独立 PQ 的第10.1节提前退出，与第14–15节联合 Needs Met 的 PQ 不能混用。运行规范明确 `standalone_pq`／`combined_nm`，联合任务中的标记不是统一退出；无法评价外语／未加载结果的 PQ 可留 N/A。辅助批量脚本明确只生成独立 PQ 批次，并在批次元数据中保存语境。

抓取正文是证据的一部分：实际布局、图片／音视频／功能、责任主体、独立声誉和必要事实核查仍按页面需要执行。没有声誉信息不同于未做调查；抓取服务返回404不同于目标页面真正没有内容。

数值编码、输出字段、缺证据状态、六项证据检查及批量整理脚本是本地执行约定，不声称为原文原生 JSON schema。平台账户、任务领取与提交按钮不属于技能评分能力。Source coverage 与结构测试不是准确率认证。

---

# Page Quality Rater 1.0.2：来源与标准对照

本文件仅用于规则维护、审阅和版本追溯，不是每次评分要加载的提示词。五档开头完整英文定义段直接保留原文，并附中文译文；初始考虑和质量条件表逐项对应翻译，补充细则另列。其他运行规则仍是中文释义及可执行组织，不声称整个技能为逐字翻译，也不代表 Google 官方认证。

## 来源与范围

- 基准：Google Search Quality Evaluator Guidelines，**September 11, 2025**。
- 主体：Part 1 第 1.0–11.0 节，印刷页 9–93；逐节核对包含表格、限制、例外、FAQ 及所有 PQ 示例。
- 补充：第 0.2–0.6 节（评分语境与访问要求）、14.0（PQ 与 Needs Met 的边界）、15.0–15.1／15.5／15.6（相关标记及加载规则）。不把 Needs Met 档位规则导入 PQ。
- 官方文档入口：[Search Quality Evaluator Guidelines](https://static.googleusercontent.com/media/guidelines.raterhub.com/en//searchqualityevaluatorguidelines.pdf)。本次以用户提供且可核验的本地版本为准，没有声称该日期是官网目前最新版本。
- 用户提供文件：`SearchQualityEvaluatorGuidelines.pdf`。
- PDF：182 页，8,969,290 bytes。
- SHA-256：`46aa9ea1fd004a09d68ececd7acdb18ec505061f52598fa69a856dfea94a6a06`。
- 本技能编制日期：2026-11-04。

正文核对使用逐页文本；另外渲染了第 19、29、30、59、62、68、69、71、77、91 页，查看关键量表、最低条件、High/Highest 表格和提前结束规则的原始版式。图片链接展示的历史网页细节不作为对当前网页的实测结论。

## 结构及读取方式

| 文件 | 作用 |
|---|---|
| [SKILL.md](../SKILL.md) | 范围、流程、五档完整定义和必要条件、四个中间档、输出边界 |
| [assessment.md](../references/assessment.md) | 每次必读的调查、MC/SC/Ads、YMYL、声誉和 E-E-A-T 细则 |
| [lowest.md](../references/lowest.md) | 每次必读的最低档触发条件与反例，不因层级拆分而成为可选规则 |
| [page-types.md](../references/page-types.md) | 前置分流及百科、问答、错误页、登录、商店、工具、媒体、PDF 例外 |
| [calibration-cases.md](../references/calibration-cases.md) | 41 个条件边界示例，用于说明标准，不是模型评测结果 |
| [output.md](../references/output.md) | 简洁输出、状态、标签与本地数值编码 |
| [coverage.json](coverage.json) | 逐节可机读映射 |

## 必须保留的精细边界

1. 第 7.0 和 8.0 的突出条件是 **at least one**，而不是所有条件同时具备；但页面目的、伤害、必要信任、责任信息与干扰等基础条件仍然适用。
2. 第 5.0 的“一个 Low 特征即可支持”与第 6.1、9.3 的混合 Medium 路径同时存在。混合路径准确保留“一个 Low 特征，或一些轻度 Low 特征”，不能把前者也限定为轻度；不能简单用 `any(low_flag)` 覆盖全部例外；也不能让混合判断抵消 Lowest 或第 5.1 的必要 E-E-A-T 缺失。
3. 第 3.3 要求声誉调查，第 3.3.5、7.2、8.2 同时明确无可查声誉不阻止高分。查不到、没查、工具不可用，不能混淆。
4. 第 3.3.1 不把流量统计当声誉；第 7.2、8.2 允许非 YMYL 的主题知名度、参与度和用户评价成为声誉证据。两者均保留。
5. 第 4.5.4 不把正规付费墙或必要登录当欺骗；第 10.1 对不可访问 MC 不给整体 PQ。第 8.4 的银行登录页 Highest，是入口页面本身，不是猜测登录后的账户内容。
6. 第 9.2 和 15.5 区分定制 404、暂时坏页、系统性无 MC、真正完全未加载、恶意警告。不能都填 0。
7. 第 4.6.4 并非第三方内容即滥用；第 4.6.5–4.6.7 并非 AI／转载即低质。目的、规模、投入、原创性和新增价值的限定不可删除。
8. 第 9.3 新问题未回复通常 Medium；有价值但混合的讨论可 Medium；可信 YMYL 生活经历可 Highest。不得简化成回复数量、实名或医生资格门槛。
9. 第 14.0 的明确网站意图例外属于 Needs Met，不抬升 PQ；PQ 与 query 无关。
10. 第 10.0 允许两档难定取低、三档难定取中，并保留有理由处理未列举情形的判断空间。这不是把信息不足机械映射成 Medium。

## 本地接口和执行约定（并非原文新增质量条件）

| 约定 | 与原标准的关系 |
|---|---|
| 五主档编码 0–4，中间档增加 0.5 | 原文规定五主档和四个 + 半档；具体数值是本地保存格式，不是质量概率或官方 API 编码。 |
| rated / not_rated / insufficient_evidence、null | 前两者承载原文可评分／前置退出；insufficient_evidence 为自动化证据不足状态。它们不是新增质量档位。 |
| live_page / supplied_page_evidence、时点和证据来源 | 用于说明观察范围与可复现性。给足实际页面和调查证据的快照可评价；仅搜索摘要不能伪装为实际 PQ。 |
| 语言、地区和文化语境 | 保留原文规则；输出中文不等于把任务地区自动设成中国，不把所有英语页标为 Foreign Language。 |
| 功能测试 | 原文要求检查实际功能，举例包括购物车；实现保留安全、授权范围内的实测，并明确未测限制，不扩大为购买或提供敏感信息。 |
| 广告过滤 | 原文要求不使用广告屏蔽来评分；实现保留未过滤体验要求，但不自动修改用户扩展或安全设置。无法还原时记限制并补证。 |
| 搜索垃圾政策外链 | 第 4.6 指向 Search Essentials。技能完整保留该 PDF 展开的技术及“已识别垃圾手法”规则；未把外链今后更新内容无版本控制地混入此基准。 |
| 案例 | 归纳关键决策边界；不逐字复制全部截图、不将案例事实当作当前网站永久标签，也不把举例属性升级为普遍必选条件。 |
| 输出简洁、批量 ID 和顺序 | 工程接口规则，不改变质量标准；不添加强制分布或维度相加。 |

## Part 1 逐节对照

以下 75 行对应该版本 Part 1 目录中的全部 75 个节标题。映射完整是防遗漏索引，不能单凭行数证明语义或模型准确率。页码为 PDF 印刷页，恰与本版本 PDF 页序一致。

| 原节 | 页 | 标题 | 实现位置与保留内容 |
|---|---:|---|---|
| 1.0 | 9 | Introduction to Page Quality Rating | [SKILL.md](../SKILL.md)：PQ 评价实际页面实现目的的程度 |
| 2.0 | 9 | Understanding Webpages and Websites | [references/assessment.md](../references/assessment.md)：理解实际页面和所属网站 |
| 2.1 | 9 | Important Definitions | [references/assessment.md](../references/assessment.md)：页面、网站、独立子站、首页、所有者和创作者；AI 是工具而非档位 |
| 2.2 | 10 | Understanding the Purpose of a Webpage | [references/assessment.md](../references/assessment.md)：有益目的、多目的、商业与帮助并存；不按内容类型设等级 |
| 2.3 | 11 | Your Money or Your Life (YMYL) Topics | [references/assessment.md](../references/assessment.md)：YMYL 四类风险、影响对象、连续性、危害判断及经验与专业要求 |
| 2.4 | 13 | Understanding Webpage Content | [references/assessment.md](../references/assessment.md)：MC、SC、Ads 的功能性划分 |
| 2.4.1 | 13 | Identifying the Main Content (MC) | [references/assessment.md](../references/assessment.md)：标题、媒体、功能、用户内容、隐藏标签均可能是 MC |
| 2.4.2 | 14 | Identifying the Supplementary Content (SC) | [references/assessment.md](../references/assessment.md)：SC 对体验的贡献及目的相关性 |
| 2.4.3 | 14 | Identifying Advertisements/Monetization (Ads) | [references/assessment.md](../references/assessment.md)：变现链接、动态广告、总体广告责任；广告有无不直接定档 |
| 2.4.4 | 14 | Summary of the Parts of the Page | [references/assessment.md](../references/assessment.md)：按实际作用组合判断，不把评论或标签固定分类 |
| 2.5 | 15 | Understanding the Website | [references/assessment.md](../references/assessment.md)：网站及创作者自述、独立证据与冲突处理 |
| 2.5.1 | 15 | Finding the Homepage | [references/assessment.md](../references/assessment.md)：相关首页、独立站点部分、多个合理首页及缺链接例外 |
| 2.5.2 | 16 | Finding Who is Responsible for the Website and Who Created the Content on the Page | [references/assessment.md](../references/assessment.md)：站点与创作者责任；机构、署名作者、账号与多人讨论 |
| 2.5.3 | 18 | Finding About Us, Contact Information, and Customer Service Information | [references/assessment.md](../references/assessment.md)：目的相关的身份、联系和客服信息；匿名、别名、支付退换及当地惯例 |
| 3.0 | 19 | Overall Page Quality Rating | [SKILL.md](../SKILL.md)：目的与伤害前置；九个刻度；原文 + 为相邻半档 |
| 3.1 | 19 | Page Quality Rating Considerations | [SKILL.md](../SKILL.md)、[references/assessment.md](../references/assessment.md)：目的、伤害、YMYL、站点类型、责任信息、MC、标题、广告、声誉和信任 |
| 3.2 | 21 | Quality of the Main Content | [references/assessment.md](../references/assessment.md)：投入、原创性、才华技能、准确性、共识、实际功能和内容形式 |
| 3.3 | 22 | Reputation of the Website and Content Creators | [references/assessment.md](../references/assessment.md)：所有可继续的正式 PQ 任务须调查独立且主题相关的声誉 |
| 3.3.1 | 22 | Reputation of the Website | [references/assessment.md](../references/assessment.md)：真实用户及专家意见，独立来源、专业语境和流量统计边界 |
| 3.3.2 | 23 | Customer Reviews as Reputation Information | [references/assessment.md](../references/assessment.md)：评价内容与可信度，正负评论都可伪造；少量普通差评不等于负面声誉 |
| 3.3.3 | 23 | How to Search for Reputation Information about a Website | [references/assessment.md](../references/assessment.md)：搜索站点、域名、机构及创作者，排除自述并核查来源 |
| 3.3.4 | 25 | Reputation of the Content Creators | [references/assessment.md](../references/assessment.md)：专业与非专业创作者，同行认可、引用、履历和非正式评论 |
| 3.3.5 | 25 | What to Do When You Find No Reputation Information | [references/assessment.md](../references/assessment.md)：无声誉中性，小站与普通人可高分，YMYL 重视其他证据 |
| 3.4 | 26 | Experience, Expertise, Authoritativeness, and Trust (E-E-A-T) | [references/assessment.md](../references/assessment.md)：E-E-A-T 四概念，Trust 优先，三类证据，利益冲突及其他信任因素 |
| 3.4.1 | 28 | YMYL Topics: Experience or Expertise? | [references/assessment.md](../references/assessment.md)：YMYL 生活经验与专家信息的目的区别及安全、共识限制 |
| 4.0 | 29 | Lowest Quality Pages | [SKILL.md](../SKILL.md)、[references/lowest.md](../references/lowest.md)：Lowest 总定义、前置必须条件及六维任一充分条件 |
| 4.1 | 31 | Types of Lowest Quality Pages | [references/lowest.md](../references/lowest.md)：有害、不可信、垃圾分类可重叠；争议和厌恶不自动最低 |
| 4.2 | 32 | Harmful to Self or Other Individuals | [references/lowest.md](../references/lowest.md)：个人伤害的完整类型、易受伤害人群、善意报道教育艺术例外 |
| 4.3 | 33 | Harmful to Specified Groups | [references/lowest.md](../references/lowest.md)：全部指定群体、严肃或恶意语气、思想批评与历史教育例外 |
| 4.4 | 34 | Harmfully Misleading Information | [references/lowest.md](../references/lowest.md)：有害误导三类、独立核查、合理争论及娱乐偏好微小错误例外 |
| 4.5 | 35 | Untrustworthy Webpages or Websites | [references/lowest.md](../references/lowest.md)：高度不可信、重大多处错误、操纵；强烈怀疑与取证边界 |
| 4.5.1 | 35 | Inadequate Information about the Website or Content Creator for the Purpose of the Page | [references/lowest.md](../references/lowest.md)：高信任责任和联系信息；完全缺失与普通不足区别 |
| 4.5.2 | 36 | Lowest E-E-A-T and Lowest Reputation of the Website or Content Creator | [references/lowest.md](../references/lowest.md)：最低 E-E-A-T、极度不专业 YMYL 和可信极负面声誉 |
| 4.5.3 | 36 | Deceptive Page Purpose, Deceptive Information about the Website, Deceptive Design | [references/lowest.md](../references/lowest.md)：目的、身份、资质、业务信息、按钮、导航和标题等欺骗 |
| 4.5.4 | 38 | Deliberately Obstructed or Obscured MC | [references/lowest.md](../references/lowest.md)：故意遮挡或淹没 MC；可信登录、订阅、地域和非故意故障例外 |
| 4.5.5 | 38 | Suspected Malicious Behavior | [references/lowest.md](../references/lowest.md)：诈骗、钓鱼、恶意下载，强烈怀疑可最低；停止访问及警告区别 |
| 4.6 | 39 | Spammy Webpages | [references/lowest.md](../references/lowest.md)：垃圾页面及已识别搜索垃圾手法；不把 SEO 或单一线索当定论 |
| 4.6.1 | 39 | Cannot Determine a Purpose, No MC, Little MC, Lowest Quality MC or No Added Value MC | [references/lowest.md](../references/lowest.md)：目的不可理解、故意无或极少 MC、最低质量及零增值 |
| 4.6.2 | 39 | Hacked, Defaced, or Spammed Pages | [references/lowest.md](../references/lowest.md)：入侵篡改及大量垃圾评论使原目的失效 |
| 4.6.3 | 40 | Expired Domain Abuse | [references/lowest.md](../references/lowest.md)：过期域名滥用定义、常见表现与必要条件的区别及历史核查 |
| 4.6.4 | 41 | Site Reputation Abuse | [references/lowest.md](../references/lowest.md)：站点声誉滥用的主要目的条件及七类非自动滥用例外 |
| 4.6.5 | 42 | Scaled Content Abuse | [references/lowest.md](../references/lowest.md)：规模化滥用的主要目的、典型低增值表现；编辑不是排除项；多页取证 |
| 4.6.6 | 42 | MC Created with Little to No Effort, Little to No Originality, and Little to No Added Value for Website Visitors | [references/lowest.md](../references/lowest.md)：全部或几乎全部复用、投入原创和增值；署名不免责、授权联供例外 |
| 4.6.7 | 43 | How to Determine if Content is Copied | [references/lowest.md](../references/lowest.md)：句段搜索、动态旧源、轻改、改写、日期与档案对比 |
| 4.7 | 45 | Examples of Lowest Quality Pages | [references/lowest.md](../references/lowest.md)、[references/calibration-cases.md](../references/calibration-cases.md)：核对全部示例，抽取事实错误、身份欺骗、伤害、垃圾及增值边界 |
| 5.0 | 59 | Low Quality Pages | [SKILL.md](../SKILL.md)：Low 定义、轻度伤害边界、各维度单项足以支持及网站类型无豁免 |
| 5.1 | 60 | Lacking E-E-A-T | [SKILL.md](../SKILL.md)、[references/assessment.md](../references/assessment.md)：必要 E-E-A-T 不足不能被声誉或站点类型补偿 |
| 5.2 | 60 | Low Quality Main Content | [SKILL.md](../SKILL.md)、[references/assessment.md](../references/assessment.md)：低投入内容、轻度事实错误、标题夸张；普通人艺术分享例外 |
| 5.2.1 | 61 | MC is Created with Low Effort, Low Originality and Low Added Value for Website Visitors | [SKILL.md](../SKILL.md)、[references/lowest.md](../references/lowest.md)：低增值复用与近零增值 Lowest 的界线 |
| 5.2.2 | 62 | Filler as a Poor User Experience | [SKILL.md](../SKILL.md)、[references/assessment.md](../references/assessment.md)：filler 的两类 Low 条件：糟糕体验或妨碍查找；有价值背景例外 |
| 5.3 | 62 | Distracting Ads/SC | [SKILL.md](../SKILL.md)、[references/assessment.md](../references/assessment.md)：明显干扰、易关闭弹窗例外、性暗示怪诞或耸动的广告 SC 内容 |
| 5.4 | 62 | Mildly Negative Reputation of the Website or Content Creator | [SKILL.md](../SKILL.md)、[references/assessment.md](../references/assessment.md)：轻度负面声誉及调查、少量评论和缺乏声誉的限制 |
| 5.5 | 63 | Unsatisfying Amount of Information about the Website or Content Creator | [SKILL.md](../SKILL.md)、[references/assessment.md](../references/assessment.md)：目的相关的责任和客服信息不足；别名例外 |
| 5.6 | 63 | Exaggerated or Mildly Misleading Information about the Website or Content Creator | [SKILL.md](../SKILL.md)、[references/lowest.md](../references/lowest.md)：轻度夸大与故意显著欺骗的 Low / Lowest 边界 |
| 5.7 | 64 | Examples of Low Quality Pages | [SKILL.md](../SKILL.md)、[references/calibration-cases.md](../references/calibration-cases.md)：核对所有低质示例；编辑错误、空泛建议、缺步骤、广告及 filler |
| 6.0 | 68 | Medium Quality Pages | [SKILL.md](../SKILL.md)：Medium 完整基础条件、适当 MC、声誉及 E-E-A-T |
| 6.1 | 69 | Types of Medium Quality Pages | [SKILL.md](../SKILL.md)：没有问题但不突出与混合质量两条路径及上下边界 |
| 6.2 | 69 | Examples of Medium Quality Pages | [references/calibration-cases.md](../references/calibration-cases.md)：百科、幽默、娱乐、食谱示例不升级为统一内容长度或评论数量门槛 |
| 7.0 | 71 | High Quality Pages | [SKILL.md](../SKILL.md)：High 基础条件与至少一项突出标准 |
| 7.1 | 72 | High Quality Main Content | [SKILL.md](../SKILL.md)、[references/assessment.md](../references/assessment.md)：高 MC 投入原创技能准确性及与平均页面区别 |
| 7.2 | 72 | Positive Reputation | [SKILL.md](../SKILL.md)、[references/assessment.md](../references/assessment.md)：主题相关正面声誉，非正式证据、平台归属与无声誉例外 |
| 7.3 | 73 | High Level of E-E-A-T | [SKILL.md](../SKILL.md)、[references/assessment.md](../references/assessment.md)：高 E-E-A-T 随目的、主题、经验及信任要求变化 |
| 7.4 | 74 | Examples of High Quality Pages | [references/calibration-cases.md](../references/calibration-cases.md)：核对新闻、官方、幽默、商家、购物、视频、游戏、百科示例，不以形式替代质量 |
| 8.0 | 77 | Highest Quality Pages | [SKILL.md](../SKILL.md)：Highest 基础条件与至少一项非常突出的标准 |
| 8.1 | 78 | Very High Quality MC | [SKILL.md](../SKILL.md)：新闻、艺术、信息内容的不同最高质量要求 |
| 8.2 | 78 | Very Positive Reputation | [SKILL.md](../SKILL.md)、[references/assessment.md](../references/assessment.md)：非常正面声誉，专家推荐／奖项与非 YMYL 证据；无声誉例外 |
| 8.3 | 78 | Very High Level of E-E-A-T | [SKILL.md](../SKILL.md)、[references/assessment.md](../references/assessment.md)：独特权威、丰富经验或很高专业能力及主题相关信任 |
| 8.4 | 79 | Examples of Highest Quality Pages | [references/page-types.md](../references/page-types.md)、[references/calibration-cases.md](../references/calibration-cases.md)：核对全部最高示例；登录、PDF、个人经验和艺术亦可最高 |
| 9.0 | 85 | Page Quality Criteria for Specific Types of Pages | [references/page-types.md](../references/page-types.md)：特殊页面规则按类型适用 |
| 9.1 | 85 | Ratings for Encyclopedia Pages | [references/page-types.md](../references/page-types.md)：百科站点声誉与条目自身研究、引用、准确性的共同要求 |
| 9.2 | 85 | Ratings for Pages with Error Messages or No MC | [references/page-types.md](../references/page-types.md)：定制错误页、临时坏页、系统性空内容、真正未加载的不同处理 |
| 9.3 | 87 | Ratings for Forums and Q&A Pages | [references/page-types.md](../references/page-types.md)：论坛 MC、访客视角、别名、新帖、长期无回复、混合质量及 YMYL 经验 |
| 10.0 | 91 | Page Quality Rating Tasks | [SKILL.md](../SKILL.md)、[references/page-types.md](../references/page-types.md)：两个档位取低、三个取中；地域不扣分；非 HTML 与未穷尽情形 |
| 10.1 | 91 | Instructions for Rating Page Quality Tasks | [references/page-types.md](../references/page-types.md)、[references/output.md](../references/output.md)：前置退出、语言与未加载定义；PQ 网格和评论转为证据记录 |
| 11.0 | 92 | Page Quality Rating FAQs | [SKILL.md](../SKILL.md)、[references/assessment.md](../references/assessment.md)、[references/calibration-cases.md](../references/calibration-cases.md)：外观不决定、个人意见、YMYL 经验、非正式专业、艺术目的、统一标准与插页 |

## 额外关联条款

| 原节 | 页 | 保留规则 |
|---|---|---|
| 0.2–0.6 | 7–8 | 地区与文化视角、不以个人偏好评分、未过滤广告的体验、安全浏览、历史示例语境 |
| 14.0 | 142–143 | PQ 不依赖 query；不得将 Needs Met 导航豁免挪用于 PQ |
| 15.0–15.1 | 144 | 标记依据结果本身，色情及显著色情广告的语境 |
| 15.5 | 149–150 | 真正未加载、定制错误页、付费限制、恶意警告的区别；不导入 Needs Met 的 FailsM 映射 |
| 15.6 | 151 | 可选内容标记不自动决定 PQ |

## 本轮严格审查

2026-11-04 对 v1.0.0 进行语义审查，修正 8 处规则表述或流程问题，并补充 4 处明示细则；修订版本为 v1.0.1。逐条旧文、原文定位、影响和修正内容见 [review-2026-11-04.json](review-2026-11-04.json)。此记录仅供维护，不随评分加载。对 16 组原文案例或条件反例进行了人工规则复核，未执行模型盲测，也未测试真实网页评分准确率。

## 五档定义逐句对照

随后对五档定义与条件表进行单独复核，形成 v1.0.2。英文定义段逐字保留在 SKILL；五档初始考虑与质量判断条件按原表逐项译成中文。原文、中文、原节、页码及核对结果见 [level-definitions-alignment.json](level-definitions-alignment.json)。73 段英文内容与固定 PDF 提取文本在忽略空白后逐字匹配，中文为人工译文，不称为英文原文。原有适用细则、限制和例外继续生效。

## 维护与验证界限

已完成基准全文对应章节阅读、逐节映射和规则边界审阅。技能结构使用 skill-creator 的 quick_validate 校验；另外检查文件引用和打包哈希。尚未进行独立模型盲测或真实网页评分校准，不能从规则完整度推导准确率或稳定性。

后续更新应固定新 PDF 的日期与哈希，逐节比较实质变化；同步检查必要条件、严重程度、否定条件、例外、例证语境及任务前置退出，而不是只搜索新增关键词。运行时标准保留在 SKILL 与 references；来源、页码及维护说明留在本目录。
