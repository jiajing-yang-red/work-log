"""User-facing failure descriptions, including older saved evaluation records."""
from quality_llm.protocols import response_failure_code


ERRORS = {
    "recovery_interrupted": ("恢复过程中断", "该轮请求已登记，但进程中断后未取得响应；是否产生费用未知。该轮仍计入自动恢复上限。"),
    "partial_recovery_conflict": ("部分评分未恢复", "本轮未能在保留已有有效评分的前提下修复失败项；已有有效评分仍保留。"),
    "input_unavailable": ("评测输入缺失", "缺少评分所需的输入或网页快照，未生成质量分数；请修正输入后重新评测。"),
    "evidence_path_missing": ("证据路径错误", "模型引用的字段不存在，无法核验证据。"),
    "evidence_quote_mismatch": ("引文不匹配", "引用与指定字段的原文不完全一致，可能改变了大小写或空白、改写原文，或引用了其他字段。"),
    "evidence_source_invalid": ("证据来源错误", "引用超出了本次允许使用的搜索结果、网页或参考答案范围。"),
    "duplicate_dependency_invalid": ("重复依赖无效", "所指向的重复组成员未通过校验，本条重复关系暂不采纳。"),
    "invalid_json": ("输出格式错误", "模型回答无法解析为评分要求的 JSON。"),
    "output_contract_invalid": ("评分输出不合规", "返回的字段、类型或评分规则未通过校验。"),
    "transport_error": ("接口连接失败", "模型请求遇到连接异常或超时。"),
    "http_error": ("接口请求失败", "模型接口返回了 HTTP 错误。"),
    "provider_rate_limited": ("接口限流（429）", "模型平台暂时限制请求；本条未取得评分。请在模型响应中查看各次尝试和等待记录，必要时降低并发或请求速率。"),
    "retry_wait_exhausted": ("限流等待超出预算", "同一模型客户端仍处于冷却或排队状态，等待时间超出预算，本条没有继续发送请求；请稍后再评或降低请求速率。"),
    "provider_payment_required": ("模型账户额度不足", "模型接口返回 HTTP 402，需检查该平台的余额或可用额度；本条没有生成评分。"),
    "incomplete_response": ("模型响应不完整", "模型回答被截断、拒绝，或缺少完整的响应内容。"),
    "stream_total_timeout": ("生成总时长超限", "接口已开始返回数据，但完整回答未在配置的生成时限内完成，程序停止接收。"),
    "stream_idle_timeout": ("等待数据超时", "流式连接长时间没有新数据，达到网络读取超时。"),
    "stream_interrupted": ("流式连接中断", "接收回答过程中连接断开，未获得完整评分正文。"),
    "stream_timeout_or_interrupted": ("流式超时或中断", "历史记录显示流式接收超时或中断；当时没有区分生成总时限与网络中断。"),
    "stream_incomplete": ("流式结束标志缺失", "流式响应未收到完整结束标志，已保留收到的部分回答。"),
    "stream_malformed": ("流式数据格式错误", "接口返回的某个流式事件无法解析，已保留此前收到的回答。"),
    "stream_provider_error": ("流式接口报错", "接口在流式响应中返回错误，具体错误见模型响应详情。"),
    "response_too_large": ("响应超出接收上限", "返回数据超过程序的接收大小限制。"),
    "output_truncated": ("输出长度超限", "模型达到输出 token 上限，正文可能不完整或为空；推理也可能耗用此预算，请查看响应详情和用量。"),
    "model_refused": ("模型拒绝回答", "接口返回拒绝或内容过滤标记；拒绝信息可在模型响应中查看。"),
    "empty_response": ("回答正文为空", "接口未返回可见回答正文，因此无法解析出评分。"),
    "evaluation_error": ("评分处理失败", "该项没有生成可用评分，具体原因见原始诊断。"),
}
LEGACY_ERRORS = {
    "Evidence path does not exist": "evidence_path_missing",
    "Evidence path must be a JSON Pointer": "evidence_path_missing",
    "Citation is not an exact substring of its source": "evidence_quote_mismatch",
    "Citation must point to supplied result, page or independent reference": "evidence_source_invalid",
    "Network request failed or timed out": "transport_error",
    "Provider response refused, incomplete or malformed": "incomplete_response",
}


def failure_info(row, diagnostics=None):
    if row["judgment_status"] != "error":
        return None
    reason = row.get("reason", "")
    code = row.get("error_code") or LEGACY_ERRORS.get(reason)
    if reason == "HTTP 402":
        code = "provider_payment_required"
    attempts = (diagnostics or {}).get("attempts") or []
    if reason == "HTTP 429" or (code == "http_error" and attempts and attempts[-1].get("http_status") == 429):
        code = "provider_rate_limited"
    if code == "incomplete_response" and diagnostics:
        attempts = diagnostics.get("attempts") or []
        if attempts:
            code = response_failure_code(attempts[-1].get("response"))
    if not code and reason.startswith("HTTP "):
        code = "http_error"
    if not code:
        code = "output_contract_invalid" if row.get("task_status") == "invalid_output" else "evaluation_error"
    label, description = ERRORS.get(code, ERRORS["evaluation_error"])
    if (diagnostics or {}).get("retry_stop_reason") == "wait_budget_exhausted" and attempts:
        description += " 所需等待超过本条剩余等待预算，已停止重试，未提前突破服务端要求的等待时间。"
    return {"code": code, "label": label, "description": description}
