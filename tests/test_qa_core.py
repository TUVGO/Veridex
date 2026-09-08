import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qa_core.data_policy import validate_reference_data
from qa_core.write_evidence import write_evidence_summary
from qa_core.write_policy import bulk_approval_ready, validate_write_policy


class CorePolicyTests(unittest.TestCase):
    def test_reference_data_is_alias_only_and_readonly(self):
        refs = validate_reference_data([{
            "alias": "VIN_FULL_HISTORY",
            "entity": "vehicle",
            "access": "readonly",
            "purpose": "linked fixture",
        }])
        self.assertEqual(refs[0]["alias"], "VIN_FULL_HISTORY")
        with self.assertRaisesRegex(ValueError, "readonly"):
            validate_reference_data([{"alias": "X", "entity": "vehicle", "access": "write"}])
        with self.assertRaisesRegex(ValueError, "真实标识"):
            validate_reference_data([{"alias": "X", "entity": "vehicle", "access": "readonly", "vin": "REAL"}])

    def test_write_policy_defaults_readonly_and_bulk_has_two_gates(self):
        self.assertEqual(validate_write_policy({}, "demo")["scope"], "readonly")
        policy = validate_write_policy({
            "write_policy": {
                "scope": "bulk_write",
                "estimated_rows": 100,
                "cleanup_required": True,
                "bulk_approval": {"approval_ref": "A-1", "approved_max_rows": 100},
            }
        }, "uat")
        with patch.dict(os.environ, {"QA_UAT_BULK_APPROVAL_REF": "wrong"}, clear=False):
            self.assertFalse(bulk_approval_ready(policy))
        with patch.dict(os.environ, {"QA_UAT_BULK_APPROVAL_REF": "A-1"}, clear=False):
            self.assertTrue(bulk_approval_ready(policy))

    def test_write_evidence_rejects_unfinished_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "test-data-registry.json").write_text(
                '{"schema_version":1,"run_id":"R","case_id":"C","records":[{"status":"active"}]}',
                encoding="utf-8",
            )
            (folder / "write-audit.jsonl").write_text(
                '{"status":"success","operation":"insert"}\n', encoding="utf-8"
            )
            summary = write_evidence_summary(folder, "R", "C")
            self.assertFalse(summary["valid"])
            self.assertEqual(summary["unfinished_records"], 1)


if __name__ == "__main__":
    unittest.main()
