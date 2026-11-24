from jsonschema import Draft202012Validator

def validate_report(report, schema, rubric, evidence):
    Draft202012Validator(schema).validate(report)
    expected = {c["id"]:c["max"] for c in rubric["criteria"]}
    if len(report["criteria"]) != len(expected) or {c["id"] for c in report["criteria"]} != set(expected):
        raise ValueError("Missing, duplicate or unknown criterion")
    available = {e["id"] for e in evidence if e["status"] == "available"}
    total = assessed_max = 0
    for c in report["criteria"]:
        score = c["score"]
        if score is not None and (type(score) is not int or not 0 <= score <= expected[c["id"]]):
            raise ValueError("Score outside criterion range")
        if not set(c["evidence_ids"]) <= available:
            raise ValueError("Unknown, stale or unreadable evidence reference")
        if score is not None:
            if not c["evidence_ids"] or not c["reason"].strip():
                raise ValueError("Scored criterion requires evidence and explanation")
            total += score
            assessed_max += expected[c["id"]]
    for item in report["improvements"]:
        if item["criterion_id"] not in expected:
            raise ValueError("Unknown improvement criterion")
    return {"earned":total, "assessed_max":assessed_max, "unassessed_max":100-assessed_max}
