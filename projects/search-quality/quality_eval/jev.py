"""Validate native Jev answerability decisions separately from generated citations."""

import math
from quality_input.common import InputError


VALIDATION_VERSION = "systemone-choice-v1"


def validate_jev_answer(answer, kind):
    if kind != "answerability":
        raise InputError("Jev only supports answerability scoring")
    answers = answer.get("answers") if isinstance(answer, dict) else None
    if not isinstance(answers, dict) or set(answers) != {"answerability"}:
        raise InputError("Jev response must contain exactly the requested answerability decision")
    decision = answers["answerability"]
    grades = {str(i) for i in (-1, 0, 1, 2, 3)}
    if (not isinstance(decision, dict) or decision.get("type") != "choice"
            or not isinstance(decision.get("choice"), str) or decision["choice"] not in grades):
        raise InputError("Jev returned an invalid answerability choice")
    probs = decision.get("probabilities")
    valid_probability = lambda p: type(p) in (int, float) and math.isfinite(p) and 0 <= p <= 1
    if (not isinstance(probs, dict) or set(probs) != grades or
            any(not valid_probability(p) for p in probs.values()) or
            not math.isclose(sum(probs.values()), 1, abs_tol=1e-3)):
        raise InputError("Jev probabilities must cover all five grades and sum to 1")
    if not valid_probability(decision.get("confidence")):
        raise InputError("Jev confidence must be a number in 0..1")
    if probs[decision["choice"]] + 1e-6 < max(probs.values()):
        raise InputError("Jev choice does not match the highest returned probability")
    measurement = {
        "status": "rated", "value": int(decision["choice"]),
        "reason": "Jev 原生 Choice 分档；接口不提供文字理由或原文引文。",
        "evidence": [], "decision": decision,
    }
    return {"status": "success", "judgment": {"dimensions": {"answerability": measurement}},
            "validation_version": VALIDATION_VERSION, "raw_output_status": "success",
            "evidence_repairs": [], "validation_issues": []}
