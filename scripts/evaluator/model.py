import json
import math
import os
import urllib.request

ENDPOINT = "https://api.openai.com/v1/responses"

def configuration(payload_bytes):
    names = ("JUDGE_MAX_USD", "JUDGE_INPUT_USD_PER_MILLION", "JUDGE_OUTPUT_USD_PER_MILLION")
    values = [float(os.environ.get(n, "0")) for n in names]
    if any(not math.isfinite(v) or v <= 0 for v in values):
        raise ValueError("Set positive finite budget and verified per-million token prices")
    model = os.environ.get("JUDGE_MODEL", "")
    if not model or not os.environ.get("JUDGE_PRICING_VERIFIED_ON"):
        raise ValueError("Set model and pricing verification date")
    limit = int(os.environ.get("JUDGE_MAX_OUTPUT_TOKENS", "6000"))
    if not 1000 <= limit <= 10000:
        raise ValueError("Output token limit must be 1000..10000")
    # Deliberately conservative byte-based estimate, not a provider billing guarantee.
    estimate = ((payload_bytes + 4096)*values[1] + limit*values[2])/1_000_000
    if estimate > values[0]:
        raise ValueError("Estimated upper cost exceeds configured run budget")
    return model, limit, estimate, values

def assess(profile, schema, rubric, packet):
    instructions = profile + "\nReturn JSON only. Rubric: " + json.dumps(rubric, ensure_ascii=False)
    content = json.dumps(packet, ensure_ascii=False)
    model, limit, estimate, rates = configuration(len((instructions+content+json.dumps(schema)).encode()))
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise ValueError("OPENAI_API_KEY is missing")
    payload = {"model":model, "store":False, "instructions":instructions, "input":content,
               "max_output_tokens":limit,
               "text":{"format":{"type":"json_schema", "name":"judge_report", "strict":True, "schema":schema}}}
    req = urllib.request.Request(ENDPOINT, json.dumps(payload).encode(),
          {"Authorization":"Bearer "+key, "Content-Type":"application/json"})
    # No retries: a timeout can happen after a billable request was accepted.
    with urllib.request.urlopen(req, timeout=180) as response:
        result = json.load(response)
    if result.get("status") != "completed":
        raise ValueError("Model response incomplete or refused")
    text = "".join(c.get("text", "") for item in result.get("output", [])
                   for c in item.get("content", []) if c.get("type") == "output_text")
    usage = result.get("usage", {})
    cost = (usage.get("input_tokens",0)*rates[1]+usage.get("output_tokens",0)*rates[2])/1_000_000
    return json.loads(text), {"model":model,"usage":usage,"estimated_ceiling_usd":estimate,
                            "calculated_cost_usd":cost,"pricing_verified_on":os.environ["JUDGE_PRICING_VERIFIED_ON"]}
