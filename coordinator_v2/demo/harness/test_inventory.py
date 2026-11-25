import json
import subprocess
import unittest


class InventoryTest(unittest.TestCase):
    items = [{"name": "pencil", "category": "office"}, {"name": "cup", "category": "kitchen"}]

    def query(self, items, category):
        call = subprocess.run(
            ["python3", "-I", "-B", "/case/store.py"],
            input=json.dumps({"items": items, "category": category}),
            capture_output=True, text=True, check=True, timeout=5,
        )
        return json.loads(call.stdout)

    def test_filters_to_selected_category(self):
        self.assertEqual(self.query(self.items, "office"), self.items[:1])

    def test_returns_no_items_for_unknown_category(self):
        self.assertEqual(self.query(self.items, "unknown"), [])

    def test_returns_no_items_for_empty_catalog(self):
        self.assertEqual(self.query([], "office"), [])
