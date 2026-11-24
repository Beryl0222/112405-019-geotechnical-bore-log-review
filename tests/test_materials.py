import json
from pathlib import Path
import unittest


class InputMaterialsTest(unittest.TestCase):
    def test_case_file_has_stable_source_records(self):
        path = Path(__file__).parents[1] / "data" / "case.json"
        case = json.loads(path.read_text(encoding="utf-8"))
        self.assertIsInstance(case["case_id"], str)
        self.assertTrue(case["case_id"])
        records = case["records"]
        self.assertGreaterEqual(len(records), 2)
        ids = [record["id"] for record in records]
        self.assertEqual(len(ids), len(set(ids)))
        for record in records:
            self.assertTrue(record["text"].strip())
            self.assertTrue(record.get("kind") or record.get("source"))


if __name__ == "__main__":
    unittest.main()
