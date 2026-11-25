"""Synthetic inventory command used by the V2 lab."""

import json
import sys


def select(items, category):
    return items


if __name__ == "__main__":
    query = json.load(sys.stdin)
    print(json.dumps(select(query["items"], query["category"])))
