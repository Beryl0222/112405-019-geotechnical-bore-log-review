"""合成资料及跨表引用检查。"""
import json
from pathlib import Path
import unittest

class MaterialsTest(unittest.TestCase):
    def setUp(self):
        self.case = json.loads((Path(__file__).parents[1]/"data/case.json").read_text(encoding="utf-8"))

    def test_sources_are_distinct_and_readable(self):
        records=self.case["records"]
        ids=[r["id"] for r in records]
        self.assertEqual(len(ids),len(set(ids)))
        self.assertTrue(self.case["case_id"])
        for r in records:
            self.assertTrue(r["text"].strip())
            self.assertTrue(r["kind"])

    def test_cross_record_references_exist(self):
        ids={r["id"] for r in self.case["records"]}
        def walk(value):
            if isinstance(value,dict):
                for k,v in value.items():
                    if k=="source": self.assertIn(v,ids)
                    elif k=="sources":
                        for source in v:self.assertIn(source,ids)
                    walk(v)
            elif isinstance(value,list):
                for v in value:walk(v)
        walk(self.case["objects"])
        walk(self.case["existing"])

    def test_business_inputs_have_unique_identifiers(self):
        for group in self.case["objects"].values():
            if not isinstance(group,list):continue
            ids=[r["id"] for r in group if isinstance(r,dict) and "id" in r]
            self.assertEqual(len(ids),len(set(ids)))
        ids=[r["id"] for r in self.case["existing"]]
        self.assertEqual(len(ids),len(set(ids)))

if __name__=="__main__":
    unittest.main()
