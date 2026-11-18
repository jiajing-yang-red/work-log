"""Small strict JSON-schema subset plus evaluation-specific invariants."""

from urllib.parse import urlsplit

from quality_input.common import InputError


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


def enum(*values):
    kinds = {type(value): name for value, name in
             ((None, "null"), (True, "boolean"), (1, "integer"), ("", "string"))}
    types = list(dict.fromkeys(kinds[type(value)] for value in values))
    return {"type": types[0] if len(types) == 1 else types, "enum": list(values)}


def array(items, **bounds):
    return {"type": "array", "items": items, **bounds}


TEXT = {"type": "string"}
NONEMPTY = {"type": "string", "minLength": 1}
NULL_TEXT = {"type": ["string", "null"]}
BOOL = {"type": "boolean"}
EVIDENCE = array(obj({"path": NONEMPTY, "quote": {"type": "string", "minLength": 1, "maxLength": 200}}),
                 maxItems=8)


def measurement(values):
    return obj({
        "status": enum("rated", "unjudgeable", "not_applicable"),
        "value": enum(*values, None),
        "reason": NONEMPTY, "evidence": EVIDENCE,
    })


def legacy_measurement(values, *, freshness=False):
    return obj({
        "status": enum("rated", "not_applicable") if freshness else enum("rated"),
        "value": enum(*values, None) if freshness else enum(*values),
        "reason": NONEMPTY, "evidence": EVIDENCE,
    })


ITEM = obj({"id": NONEMPTY, "text": NONEMPTY})
INTENT = obj({
    "id": NONEMPTY, "likelihood": enum("primary", "common", "reasonable_minor", "unlikely"),
    "goal": NONEMPTY,
    "kind": enum("navigation", "resource", "fact", "research", "action", "other"),
    "requirements": array(ITEM, minItems=1, maxItems=15),
    "hard_constraints": array(ITEM, maxItems=15),
    "fully_meets_eligible": BOOL, "specific_target": NULL_TEXT,
})
SPEC_SCHEMA = obj({
    "intents": array(INTENT, minItems=1, maxItems=4),
    "freshness_applicable": BOOL,
    "freshness_type": enum("F1", "F2", "F3", "F4", "F5"),
    "needs_clarification": BOOL, "clarification_reason": TEXT,
})
ASSESSMENT = {
    "selected_intent_id": NULL_TEXT,
    "requirements": array(obj({"id": NONEMPTY, "status": enum("met", "partial", "not_demonstrated", "unknown")})),
    "constraints": array(obj({"id": NONEMPTY, "status": enum("pass", "fail", "unknown")})),
    "temporal_fit": enum("meets", "fails", "unknown", "not_applicable"),
    "reliability": enum("adequate", "insufficient", "contradicted", "unknown", "not_applicable"),
}
LEGACY_SCHEMA = obj({
    **ASSESSMENT,
    "authority_tier": enum("S1", "S2", "S3", "S4", "S5"),
    "dimensions": obj({
        "answerability": legacy_measurement([-1, 0, 1, 2, 3]),
        "authoritative": legacy_measurement([True, False]),
        "freshness": legacy_measurement([True, False], freshness=True),
        "noise": legacy_measurement([True, False]),
    }),
})
NM_SCHEMA = obj({
    **ASSESSMENT,
    "dimensions": obj({"needs_met": measurement(["FailsM", "SM", "MM", "HM", "FullyM"])}),
})
PQ_SCHEMA = obj({
    "page_purpose": TEXT,
    "dimensions": obj({"page_quality": measurement(["Lowest", "Low", "Medium", "High", "Highest"])}),
})
DUP_SCHEMA = obj({
    "rows": array(obj({
        "result_id": NONEMPTY, "status": enum("rated"),
        "duplicate_of": NULL_TEXT, "reason": NONEMPTY, "evidence": EVIDENCE,
    })),
})
ANSWERABILITY_SCHEMA = obj({
    "dimensions": obj({"answerability": legacy_measurement([-1, 0, 1, 2, 3])}),
})
SCHEMAS = {"query_spec": SPEC_SCHEMA, "legacy_url": LEGACY_SCHEMA,
           "answerability": ANSWERABILITY_SCHEMA,
           "google_nm": NM_SCHEMA, "google_pq": PQ_SCHEMA, "duplicates": DUP_SCHEMA}

# Direct judging records the judge's interpretation in the same scoring response.
# It does not create a shared query spec or require a separate preparation call.
DIRECT_ASSESSMENT = {
    "query_assessment": obj({
        "interpretation": NONEMPTY,
        "kind": INTENT["properties"]["kind"],
        "likelihood": INTENT["properties"]["likelihood"],
        "specific_target": NULL_TEXT,
        "fully_meets_eligible": BOOL,
        "needs_clarification": BOOL,
        "freshness_applicable": BOOL,
    }),
    "requirements": array(obj({
        "text": NONEMPTY, "status": enum("met", "partial", "not_demonstrated", "unknown"),
    }), minItems=1, maxItems=15),
    "constraints": array(obj({
        "text": NONEMPTY, "status": enum("pass", "fail", "unknown"),
    }), maxItems=15),
    "temporal_fit": ASSESSMENT["temporal_fit"],
    "reliability": ASSESSMENT["reliability"],
}
DIRECT_SCHEMAS = {
    "legacy_url": obj({**DIRECT_ASSESSMENT,
                       "query_assessment": obj({
                           "interpretation": NONEMPTY, "freshness_applicable": BOOL,
                           "freshness_type": SPEC_SCHEMA["properties"]["freshness_type"],
                       }),
                       "authority_tier": LEGACY_SCHEMA["properties"]["authority_tier"],
                       "dimensions": LEGACY_SCHEMA["properties"]["dimensions"]}),
    "google_nm": obj({**DIRECT_ASSESSMENT, "dimensions": NM_SCHEMA["properties"]["dimensions"]}),
}


def schema_for(kind, query_understanding="frozen"):
    if kind == "block_needs_met":
        from .block_needs_met import output_schema, COMPACT_CONTRACT
        return output_schema(COMPACT_CONTRACT)
    if query_understanding == "direct" and kind in DIRECT_SCHEMAS:
        return DIRECT_SCHEMAS[kind]
    return SCHEMAS[kind]


def validate_schema(value, schema, path="$"):
    if "enum" in schema and not any(type(value) is type(v) and value == v for v in schema["enum"]):
        raise InputError(f"{path}: value not in declared enum")
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        match = {"object": isinstance(value, dict), "array": isinstance(value, list),
                 "string": isinstance(value, str), "boolean": type(value) is bool,
                 "integer": type(value) is int, "null": value is None}
        if not any(match.get(kind, False) for kind in types):
            raise InputError(f"{path}: wrong JSON type")
    if isinstance(value, dict) and "properties" in schema:
        if set(value) != set(schema["properties"]):
            raise InputError(f"{path}: missing or unexpected keys")
        for key, field_schema in schema["properties"].items():
            validate_schema(value[key], field_schema, path + "/" + key)
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", float("inf")):
            raise InputError(f"{path}: array size out of bounds")
        for index, item in enumerate(value):
            validate_schema(item, schema.get("items", {}), f"{path}/{index}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", float("inf")):
            raise InputError(f"{path}: string length out of bounds")


def resolve_pointer(value, pointer):
    if not pointer.startswith("/"):
        raise InputError("Evidence path must be a JSON Pointer")
    try:
        for token in pointer[1:].split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            if isinstance(value, list):
                # JSON Pointer array indices are zero-based non-negative integers.
                if not token.isascii() or not token.isdecimal() or str(int(token)) != token:
                    raise ValueError("Invalid array index")
                value = value[int(token)]
            else:
                value = value[token]
    except (ValueError, KeyError, IndexError, TypeError):
        raise InputError("Evidence path does not exist")
    return value


class EvidenceError(InputError):
    def __init__(self, code, message, citation):
        super().__init__(message)
        self.code = code
        self.citation = dict(citation)


def validate_citations(evidence, payload):
    for citation in evidence:
        path = citation["path"]
        if not any(path.startswith(root) for root in ("/result/", "/page/", "/results/", "/reference_answer/")):
            raise EvidenceError("evidence_source_invalid",
                                "Citation must point to supplied result, page or independent reference", citation)
        try:
            source = resolve_pointer(payload, path)
        except InputError as exc:
            raise EvidenceError("evidence_path_missing", str(exc), citation) from exc
        if not isinstance(source, str) or citation["quote"] not in source:
            raise EvidenceError("evidence_quote_mismatch",
                                "Citation is not an exact substring of its source", citation)


def validate_spec(spec):
    validate_schema(spec, SPEC_SCHEMA)
    ids = [intent["id"] for intent in spec["intents"]]
    if len(set(ids)) != len(ids):
        raise InputError("Duplicate intent ID")
    for intent in spec["intents"]:
        for field in ("requirements", "hard_constraints"):
            keys = [item["id"] for item in intent[field]]
            if len(keys) != len(set(keys)):
                raise InputError("Duplicate requirement/constraint ID")
        if intent["fully_meets_eligible"]:
            if (len(spec["intents"]) != 1 or intent["kind"] not in ("navigation", "resource")
                    or not intent["specific_target"] or spec["needs_clarification"]):
                raise InputError("Google Overview FullyM eligibility requires one explicit specific target")


def validate_assessment(answer, payload, *, legacy=False):
    if payload.get("query_understanding") == "direct":
        assessment = answer["query_assessment"]
        if legacy:
            return assessment
        if assessment["fully_meets_eligible"] and (
                assessment["kind"] not in ("navigation", "resource") or
                not (assessment["specific_target"] or "").strip() or
                assessment["needs_clarification"] or
                assessment["likelihood"] != "primary"):
            raise InputError("Direct FullyM eligibility requires a clear single specific target")
        if assessment["needs_clarification"] and any(
                d["status"] == "rated" for d in answer["dimensions"].values()):
            raise InputError("Unresolved query ambiguity cannot receive a rated URL assessment")
        if assessment["freshness_applicable"] and not payload.get("evaluation_as_of"):
            if any(d["status"] == "rated" for d in answer["dimensions"].values()):
                raise InputError("Time-sensitive direct judgment requires evaluation_as_of")
        return assessment
    spec = payload["query_spec"]
    intents = {intent["id"]: intent for intent in spec["intents"]}
    selected = answer["selected_intent_id"]
    if selected is None:
        if answer["requirements"] or answer["constraints"]:
            raise InputError("No selected intent but requirement checks are present")
        if any(d["status"] == "rated" for d in answer["dimensions"].values()):
            raise InputError("Rated URL assessment requires a selected frozen intent")
        return None
    if selected not in intents:
        raise InputError("Selected intent is not in the frozen query specification")
    intent = intents[selected]
    for output, source in (("requirements", "requirements"), ("constraints", "hard_constraints")):
        ids = [item["id"] for item in answer[output]]
        if len(ids) != len(set(ids)) or set(ids) != {item["id"] for item in intent[source]}:
            raise InputError(f"{output} must cover exactly the selected intent's frozen items")
    return intent


def validate_answer(kind, answer, payload, authority_domains=(), *, check_citations=True):
    validate_schema(answer, schema_for(kind, payload.get("query_understanding", "frozen")))
    if kind == "query_spec":
        validate_spec(answer)
        return
    if kind == "duplicates":
        validate_duplicates(answer, payload, check_citations=check_citations)
        return
    dimensions = answer["dimensions"]
    for name, item in dimensions.items():
        if (item["status"] == "rated") != (item["value"] is not None):
            raise InputError(f"{name}: value and judgment status disagree")
        if item["status"] == "not_applicable" and name != "freshness":
            raise InputError("Only freshness supports not_applicable")
        if check_citations:
            validate_citations(item["evidence"], payload)
        positive = item["status"] == "rated" and (
            (type(item["value"]) is int and item["value"] >= 2)
            or (name in ("authoritative", "freshness") and item["value"] is True)
            or item["value"] in ("MM", "HM", "FullyM", "High", "Highest"))
        if positive and not any(e["path"].startswith(("/result/", "/page/")) for e in item["evidence"]):
            raise InputError("Positive judgment requires result/page evidence, not just reference facts")
    if kind == "answerability":
        if dimensions["answerability"]["value"] == -1 and not dimensions["answerability"]["evidence"]:
            raise InputError("Confirmed misleading content requires an actual evidence citation")
        return
    if kind == "google_pq":
        if dimensions["page_quality"]["status"] == "rated" and not answer["page_purpose"].strip():
            raise InputError("PQ requires a stated page purpose")
        return
    intent = validate_assessment(answer, payload, legacy=kind == "legacy_url")
    if kind == "legacy_url":
        grade = dimensions["answerability"]["value"]
        auth, fresh, noise = (dimensions[k] for k in ("authoritative", "freshness", "noise"))
        tier = answer["authority_tier"]
        if auth["status"] == "rated" and (tier is None or auth["value"] != (tier in ("S1", "S2"))):
            raise InputError("Authority tier and boolean disagree")
        if tier == "S1":
            hostname = (urlsplit(payload["result"]["url"] or "").hostname or "").lower()
            if not any(hostname == domain or hostname.endswith("." + domain) for domain in authority_domains):
                raise InputError("S1 requires a domain in the supplied maintained approval list")
        applicable = (answer["query_assessment"]["freshness_applicable"]
                      if payload.get("query_understanding") == "direct"
                      else payload["query_spec"]["freshness_applicable"])
        freshness_type = (answer["query_assessment"]["freshness_type"]
                          if payload.get("query_understanding") == "direct"
                          else payload["query_spec"]["freshness_type"])
        if freshness_type in ("F1", "F2", "F3", "F4") and not applicable:
            raise InputError("Legacy F1-F4 queries require freshness applicability")
        if not applicable and fresh["status"] != "not_applicable":
            raise InputError("Query without time requirement requires freshness not_applicable")
        if applicable and fresh["status"] == "not_applicable":
            raise InputError("Time-sensitive query cannot exclude results from freshness applicability")
        if fresh["value"] is True and answer["temporal_fit"] != "meets":
            raise InputError("Fresh result requires confirmed temporal fit")
        if grade in (-1, 0) and grade is not None:
            if noise["status"] != "rated" or noise["value"] is not True:
                raise InputError("Legacy grade 0/-1 requires noise=true")
            if applicable and (fresh["status"] != "rated" or fresh["value"] is not False):
                raise InputError("Unrelated/wrong legacy results must fail applicable freshness")
        if grade in (2, 3):
            if any(c["status"] != "pass" for c in answer["constraints"]):
                raise InputError("Strong legacy grade requires confirmed hard constraints")
            if any(r["status"] != "met" for r in answer["requirements"]):
                raise InputError("Legacy grade 2 is a complete answer with ambiguity, not partial credit")
            if applicable and (fresh["value"] is not True or answer["temporal_fit"] != "meets"):
                raise InputError("Strong legacy grade requires confirmed query time requirements")
            if answer["temporal_fit"] == "fails" or answer["reliability"] not in ("adequate", "not_applicable"):
                raise InputError("Strong legacy grade cannot contradict answer correctness or temporal fit")
        if grade == 3 and noise["value"] is True:
            raise InputError("Clean complete legacy answer cannot also be noise")
        if grade == -1 and not dimensions["answerability"]["evidence"]:
            raise InputError("Confirmed misleading content requires an actual evidence citation")
    if kind == "google_nm":
        value = dimensions["needs_met"]["value"]
        if any(c["status"] == "fail" for c in answer["constraints"]) and value not in (None, "FailsM"):
            raise InputError("Confirmed necessary-condition failure must be FailsM")
        if intent and intent["likelihood"] == "unlikely" and value in ("MM", "HM", "FullyM"):
            raise InputError("Unlikely interpretation cannot exceed SM")
        if value == "FullyM":
            if not intent or not intent["fully_meets_eligible"]:
                raise InputError("FullyM not eligible in frozen query specification")
            if any(c["status"] != "pass" for c in answer["constraints"]):
                raise InputError("FullyM requires all necessary constraints confirmed")
            if any(r["status"] != "met" for r in answer["requirements"]):
                raise InputError("FullyM requires complete support")
            if answer["temporal_fit"] not in ("meets", "not_applicable"):
                raise InputError("FullyM requires temporal support")
            if answer["reliability"] not in ("adequate", "not_applicable"):
                raise InputError("FullyM requires adequate reliability")


def validate_duplicates(answer, payload, *, check_citations=True):
    results = payload["results"]
    order = {result["result_id"]: i for i, result in enumerate(results)}
    ids = [row["result_id"] for row in answer["rows"]]
    if len(set(ids)) != len(ids) or set(ids) != set(order):
        raise InputError("Duplicate judgments must cover each supplied result once")
    by_id = {row["result_id"]: row for row in answer["rows"]}
    seen_urls = {}
    for result in results:
        row = by_id[result["result_id"]]
        earlier = row["duplicate_of"]
        if row["status"] == "unjudgeable" and earlier is not None:
            raise InputError("Unknown duplication cannot name a confirmed duplicate")
        if earlier is not None and (earlier not in order or order[earlier] >= order[row["result_id"]]):
            raise InputError("Duplicate must refer to an earlier result in this response")
        if check_citations:
            validate_citations(row["evidence"], payload)
        url = result.get("url")
        if url and url in seen_urls:
            if row["status"] != "rated" or earlier is None:
                raise InputError("Exact repeated URLs must retain duplication")
            root = seen_urls[url]
            visited = set()
            while by_id[root]["duplicate_of"] is not None:
                if root in visited:
                    raise InputError("Duplicate cluster has a cycle")
                visited.add(root)
                root = by_id[root]["duplicate_of"]
                if root not in order:
                    raise InputError("Duplicate root is outside the response")
            if earlier != root:
                raise InputError("Duplicate must refer to the first cluster occurrence")
        if earlier is not None:
            if by_id[earlier]["duplicate_of"] is not None:
                raise InputError("Duplicate pointer must name the first cluster member")
            if not row["evidence"]:
                raise InputError("Duplicate assertion requires source evidence")
        if not order[row["result_id"]] and (earlier is not None or row["status"] != "rated"):
            raise InputError("First result cannot be a duplicate within its list")
        if url:
            seen_urls.setdefault(url, row["result_id"])
