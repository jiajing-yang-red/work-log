# Skills

每个目录是一套可独立使用的 skill。打开对应的 `SKILL.md` 可查看输入、评估范围及输出要求。按需将整个目录复制到自己的 skills 目录，保留其中的脚本和参考文件。

| Skill | 适用场景 |
| --- | --- |
| [needs-met-rater](needs-met-rater/SKILL.md) | 依据查询、标题、摘要和 URL，判断读者仅看结果块就能获得多少帮助。 |
| [needs-met-standard-rater](needs-met-standard-rater/SKILL.md) | 结合查询意图和实际落地页评估需求满足度，也支持直接在结果块内完成需求的场景。 |
| [page-quality-rater](page-quality-rater/SKILL.md) | 评估页面本身的质量，结合主要内容、创作者、网站声誉和可信度给出判断。 |
| [url-body-fetcher](url-body-fetcher/SKILL.md) | 批量提取 HTML 或 PDF 正文，记录来源和抓取状态，并支持失败后续跑。 |
| [ai-news-skill](ai-news-skill/SKILL.md) | 汇总 AI 新闻、开源项目和社区讨论。保留 frankzch 的原始署名和 MIT 许可证。[上游项目](https://github.com/frankzch/ai-news-skill) · [许可证](ai-news-skill/LICENSE) |

例如，从仓库根目录执行下面的命令，可以在本机安装结果块评分 skill。

```bash
mkdir -p ~/.codex/skills
cp -R skills/needs-met-rater ~/.codex/skills/
```

正文抓取脚本的 Python 依赖列在 [requirements.txt](url-body-fetcher/scripts/requirements.txt)。AI 资讯脚本的依赖和用法见[中文说明](ai-news-skill/README.zh-CN.md)。
