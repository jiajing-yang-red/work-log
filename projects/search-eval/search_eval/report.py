"""Self-contained HTML report; no network assets or dependencies."""

from html import escape


def aggregate(queries):
    keys = next(iter(queries.values()))["metrics"].keys()
    return {key.replace("AP", "MAP", 1) if key.startswith("AP") else key:
            sum(query["metrics"][key] for query in queries.values()) / len(queries)
            for key in keys}


def html_report(report):
    config = report["config"]
    summary = report["summary"]
    comparison = report.get("comparison")
    metric_names = list(summary)

    def value(number):
        return f"{number:.4f}"

    def delta(number):
        css = "up" if number > 0 else "down" if number < 0 else ""
        return f'<span class="{css}">{number:+.4f}</span>'

    cards = "".join(
        '<div class="card"><span>' + escape(name) + '</span><strong>' + value(number)
        + '</strong>' + (delta(comparison["summary_delta"][name]) if comparison else '') + '</div>'
        for name, number in summary.items())
    head = '<th>查询</th><th>分类</th>' + ''.join('<th>' + escape(name) + '</th>' for name in metric_names)
    query_rows = []
    details = []
    for query_id, query in report["queries"].items():
        label = query["query"] or query_id
        cells = ''.join('<td class="num">' + value(query["metrics"][name.replace("MAP", "AP", 1)]) + '</td>'
                        for name in metric_names)
        query_rows.append('<tr><td>' + escape(label) + '<small>' + escape(query_id) + '</small></td><td>'
                          + escape(query["category"]) + '</td>' + cells + '</tr>')
        result_rows = ''.join('<tr><td>' + str(i) + '</td><td>' + escape(item["doc_id"]) + '</td><td>'
                              + ('未标注（按 0 分）' if item["grade"] is None else str(item["grade"]))
                              + '</td></tr>' for i, item in enumerate(query["ranking"], 1))
        cutoff_rows = ''.join('<tr><td>' + k + '</td><td>' + str(info["returned"]) + '</td><td>'
                              + str(info["relevant_hits"]) + '</td><td>' + str(info["judged"]) + '</td><td>'
                              + str(info["unjudged"]) + '</td></tr>' for k, info in query["cutoffs"].items())
        details.append('<details><summary>' + escape(label) + ' <small>' + escape(query_id) + '</small></summary>'
                       + '<p>已标注相关文档 ' + str(query["relevant_total"]) + ' 篇；返回 '
                       + str(query["returned_total"]) + ' 条。</p><div class="two"><div><table><thead>'
                       + '<tr><th>排名</th><th>文档 ID</th><th>等级</th></tr></thead><tbody>'
                       + (result_rows or '<tr><td colspan="3">无搜索结果</td></tr>') + '</tbody></table></div><div>'
                       + '<table><thead><tr><th>K</th><th>返回</th><th>命中</th><th>已标注</th><th>未标注</th></tr>'
                       + '</thead><tbody>' + cutoff_rows + '</tbody></table></div></div></details>')
    group_rows = ''.join('<tr><td>' + escape(name) + '</td><td>' + str(group["query_count"]) + '</td>'
                         + ''.join('<td class="num">' + value(group["metrics"][metric]) + '</td>' for metric in metric_names)
                         + '</tr>' for name, group in report["groups"].items())
    coverage_rows = ''.join(
        '<tr><td>' + k + '</td><td>' + str(item["returned"]) + '</td><td>' + str(item["judged"]) + '</td><td>'
        + str(item["unjudged"]) + '</td><td>'
        + (f'{item["judged_fraction_of_returned"]:.1%}' if item["returned"] else '无返回结果') + '</td></tr>'
        for k, item in report["coverage"].items())
    warnings = ''.join('<li>' + escape(warning) + '</li>' for warning in report["warnings"])
    baseline_section = ''
    if comparison:
        bad_rows = ''.join('<tr><td>' + escape(row["query_id"]) + '</td><td>'
                           + escape(report["queries"][row["query_id"]]["query"]) + '</td><td>'
                           + delta(row["delta"]) + '</td></tr>' for row in comparison["regressions"])
        baseline_section = '<h2>与基线比较</h2><p>基线：' + escape(comparison["baseline_name"]) + '；排序指标：' \
            + escape(comparison["regression_metric"]) + '。差值为本次分数减基线分数，采用 0–1 分值。</p>' \
            + '<table><thead><tr><th>查询 ID</th><th>查询文本</th><th>退步幅度</th></tr></thead><tbody>' \
            + (bad_rows or '<tr><td colspan="3">该指标没有退步的查询</td></tr>') + '</tbody></table>'
    return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>搜索离线评测报告</title><style>
:root{color-scheme:light}*{box-sizing:border-box}body{margin:0;background:#f5f7fb;color:#17243b;font:15px/1.65 system-ui,-apple-system,"PingFang SC",sans-serif}main{max-width:1320px;margin:auto;padding:42px 28px}h1{font-size:30px;margin:0 0 8px}h2{font-size:21px;margin:32px 0 14px}p{color:#506078}small{display:block;color:#718096;font-size:12px} .meta{background:#fff;padding:18px 22px;border:1px solid #e1e7f0;border-radius:12px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:12px;margin:24px 0}.card{background:#fff;padding:16px;border:1px solid #e1e7f0;border-radius:10px}.card span{font-size:13px;color:#506078}.card strong{display:block;font-size:25px}.card span.up,.up{color:#047857}.card span.down,.down{color:#b42318}.scroll{overflow:auto;border:1px solid #dfe5ed;border-radius:10px;background:#fff}table{border-collapse:collapse;width:100%;background:#fff;font-size:14px}td,th{text-align:left;padding:10px 14px;border-bottom:1px solid #e4e9f0;vertical-align:top}th{background:#edf2fb;font-weight:600;white-space:nowrap}.num{font-variant-numeric:tabular-nums;white-space:nowrap}input{padding:10px 14px;width:min(100%,420px);border:1px solid #ccd5e3;border-radius:8px;margin-bottom:14px;font:inherit}details{padding:14px 18px;background:white;border:1px solid #dfe5ed;border-radius:8px;margin:10px 0}summary{cursor:pointer;font-weight:600}.two{display:grid;grid-template-columns:1fr 1fr;gap:20px}.notice{border-left:3px solid #d19a21;background:#fff9e9;padding:12px 20px}.hash{overflow-wrap:anywhere;font-family:monospace;font-size:12px}@media(max-width:700px){main{padding:24px 14px}.two{grid-template-columns:1fr}}
</style><main>''' + '<h1>搜索离线评测报告</h1><p>' + escape(report["run_name"]) + '</p><div class="meta">' \
        + '查询数：<b>' + str(report["query_count"]) + '</b> · K：' + ', '.join(map(str, config["ks"])) \
        + ' · 相关阈值：≥ ' + str(config["threshold"]) + ' · NDCG 增益：' + escape(config["gain"]) \
        + '<small>运行时间 ' + escape(report["created_at"]) + ' · 评测器 ' + escape(report["evaluator_version"]) + '</small></div>' \
        + '<div class="cards">' + cards + '</div><div class="notice"><b>计算口径</b><br>' \
        + '各查询等权平均；F1 为逐查询 F1 的平均。MAP 基于输入文件完整排名，MAP@K 的 AP 分母为全部已标注相关文档数。' \
        + '不足 K 条时 Precision 仍除以 K。未标注按 0 分；未返回结果的查询仍参与统计。无二值相关文档时 Recall/AP/F1 记 0，IDCG 为 0 时 NDCG 记 0。' \
        + '</div>' + ('<ul>' + warnings + '</ul>' if warnings else '') \
        + '<h2>标注覆盖率</h2><table><thead><tr><th>K</th><th>实际返回</th><th>已标注</th><th>未标注</th><th>已标注 / 实际返回</th></tr></thead><tbody>' \
        + coverage_rows + '</tbody></table>' + baseline_section \
        + '<h2>逐查询指标</h2><input id="filter" placeholder="按查询文本、ID 或分类筛选" aria-label="筛选查询"><div class="scroll"><table id="queries"><thead><tr>' \
        + head + '</tr></thead><tbody>' + ''.join(query_rows) + '</tbody></table></div>' \
        + '<h2>按查询分类汇总</h2><div class="scroll"><table><thead><tr><th>分类</th><th>查询数</th>' \
        + ''.join('<th>' + escape(metric) + '</th>' for metric in metric_names) \
        + '</tr></thead><tbody>' + group_rows + '</tbody></table></div><h2>结果与标注明细</h2>' + ''.join(details) \
        + '<h2>可复现信息</h2><p class="hash">标注 SHA256：' + report["inputs"]["qrels_sha256"] \
        + '<br>结果 SHA256：' + report["inputs"]["results_sha256"] + '</p>' \
        + '<p>本报告衡量的是相对于当前标注集的质量。缺少相关文档标注会影响 Recall 和 MAP；请结合标注覆盖率解读结果。</p>' \
        + '''</main><script>document.getElementById('filter').addEventListener('input',function(){const q=this.value.toLowerCase();document.querySelectorAll('#queries tbody tr').forEach(r=>{r.hidden=!r.textContent.toLowerCase().includes(q)})});</script></html>'''
