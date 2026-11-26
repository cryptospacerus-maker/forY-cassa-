"""Repeatable agent responses for the unpaid synthetic V2 integration test."""

from copy import deepcopy

from .engine import FIXTURE_PLAN

TITLE = "Фильтр синтетического каталога"
SNAPSHOT = {"title": TITLE, "body": "Фильтр по категории, пустое состояние и сброс."}

CLIENT = """const category = document.querySelector('#category');
const list = document.querySelector('#items');
const empty = document.querySelector('#empty');
const inventory = [
  { name: 'pencil', category: 'office' },
  { name: 'paper', category: 'office' },
  { name: 'cup', category: 'kitchen' },
];
function render() {
  const selected = inventory.filter(item => category.value === 'all' || item.category === category.value);
  list.replaceChildren(...selected.map(item => {
    const entry = document.createElement('li');
    entry.textContent = item.name;
    return entry;
  }));
  empty.hidden = selected.length !== 0;
}
category.addEventListener('change', render);
render();
"""

STORE = '''"""Synthetic inventory command used by the V2 lab."""

import json
import sys


def select(items, category):
    if category == "all":
        return items
{fault}    return [item for item in items if item["category"] == category]


if __name__ == "__main__":
    query = json.load(sys.stdin)
    print(json.dumps(select(query["items"], query["category"])))
'''


def plan(scenario):
    result = deepcopy(FIXTURE_PLAN)
    if scenario == "dependency":
        result["tasks"][1]["depends_on"] = ["developer"]
    return result


def model(role, instruction, payload, contract):
    if role == "tester":
        defect = any('category == "unknown"' in (content or "") for content in payload["files"].values())
        return {"answer": {"sha": payload["sha"], "verdict": "fail" if defect else "pass",
                           "findings": "Unknown categories must return no items." if defect else "Fixture verdict."}}
    path, = payload["allowed_paths"]
    if role == "developer":
        defect = '' if payload["feedback"] else '    if category == "unknown":\n        return items\n'
        contents = STORE.format(fault=defect)
    else:
        contents = CLIENT
    return {"answer": {
        "summary": f"Synthetic {role} handoff", "files": [{"path": path, "content": contents}],
        "check_ids": payload["allowed_checks"],
    }}
