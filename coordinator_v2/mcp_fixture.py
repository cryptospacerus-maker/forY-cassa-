"""Synthetic read-only MCP provider. Run as a subprocess using stdio."""

import json
import sys

DATA = {"office": ["pencil", "paper"], "kitchen": ["cup"], "outdoor": ["lamp"]}

initialized = False
for line in sys.stdin:
    request = json.loads(line)
    ident = request.get("id")
    method = request.get("method")
    arguments = request.get("params", {})
    if method == "notifications/initialized":
        initialized = True
        continue
    if method == "initialize" and arguments.get("protocolVersion") == "2025-11-25":
        result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fixture-inventory", "version": "1"}}
    elif initialized and method == "tools/list":
        result = {"tools": [{"name": "inventory_lookup", "description": "Read synthetic inventory",
                             "inputSchema": {"type": "object", "additionalProperties": False,
                                             "properties": {"category": {"enum": sorted(DATA)}},
                                             "required": ["category"]}}]}
    elif initialized and method == "tools/call" and arguments.get("name") == "inventory_lookup":
        category = arguments.get("arguments", {}).get("category")
        if category not in DATA:
            result = {"isError": True, "content": [{"type": "text", "text": "Invalid category"}]}
        else:
            result = {"content": [{"type": "text", "text": "Synthetic inventory"}],
                      "structuredContent": {"category": category, "items": DATA[category]}}
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": ident,
                          "error": {"code": -32601, "message": "Unknown method"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": ident, "result": result}), flush=True)
