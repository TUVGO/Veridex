import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from uat_write import GuardedUATWriter


class FakeExecutor:
    def __init__(self):
        self.rows = {
            "uat.test_table": {
                json.dumps({"id": "ORIGINAL"}, sort_keys=True): {"id": "ORIGINAL", "value": 1}
            }
        }

    @staticmethod
    def key(identity):
        return json.dumps(identity, sort_keys=True)

    def __call__(self, action):
        table = action["table"]
        store = self.rows.setdefault(table, {})
        op = action["operation"]
        if op == "exists":
            found = [item for item in action["identities"] if self.key(item) in store]
            return {"identities": found}
        if op == "insert":
            for row in action["rows"]:
                identity = {field: row[field] for field in action["key_fields"]}
                store[self.key(identity)] = dict(row)
            return {"affected_rows": len(action["rows"])}
        if op == "update":
            for identity in action["identities"]:
                store[self.key(identity)].update(action["changes"])
            return {"affected_rows": len(action["identities"])}
        if op == "matches":
            matched = []
            for identity in action["identities"]:
                row = store.get(self.key(identity))
                if row and all(row.get(k) == v for k, v in action["expected"].items()):
                    matched.append(identity)
            return {"identities": matched}
        if op == "delete":
            affected = 0
            for identity in action["identities"]:
                if store.pop(self.key(identity), None) is not None:
                    affected += 1
            return {"affected_rows": affected}
        raise AssertionError(op)


class UATWriteTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="qa-uat-write-"))
        self.addCleanup(shutil.rmtree, self.root)
        self.executor = FakeExecutor()

    def env(self, **updates):
        values = {
            "QA_ENVIRONMENT": "uat",
            "QA_RUN_ID": "RUN-1",
            "QA_CASE_ID": "C1",
            "QA_WRITE_SCOPE": "test_data_mutate",
            "QA_TEST_DATA_REGISTRY": str(self.root / "registry.json"),
            "QA_UAT_WRITE_AUDIT_PATH": str(self.root / "audit.jsonl"),
            "QA_UAT_WRITE_ENABLED": "1",
            "QA_UAT_WRITABLE_TABLES": "uat.test_table",
            "QA_UAT_BULK_THRESHOLD": "10",
            "QA_ESTIMATED_WRITE_ROWS": "10",
        }
        values.update(updates)
        return values

    def test_owned_insert_update_delete_is_audited_and_original_stays_unchanged(self):
        with patch.dict(os.environ, self.env(), clear=False):
            writer = GuardedUATWriter.from_env(self.executor)
            writer.insert("uat.test_table", ["id"], [{"id": "QA-1", "value": "secret-value"}])
            writer.update("uat.test_table", ["id"], [{"id": "QA-1"}], {"value": 2})
            writer.delete("uat.test_table", ["id"], [{"id": "QA-1"}])
            writer.assert_clean()
        original = self.executor.rows["uat.test_table"][json.dumps({"id": "ORIGINAL"}, sort_keys=True)]
        self.assertEqual(original["value"], 1)
        registry = json.loads((self.root / "registry.json").read_text(encoding="utf-8"))
        self.assertTrue(all(item["status"] == "deleted" for item in registry["records"]))
        audit = (self.root / "audit.jsonl").read_text(encoding="utf-8")
        self.assertIn('"status": "success"', audit)
        self.assertNotIn("secret-value", audit)

    def test_original_or_preexisting_identity_cannot_be_claimed_updated_or_deleted(self):
        with patch.dict(os.environ, self.env(), clear=False):
            writer = GuardedUATWriter.from_env(self.executor)
            with self.assertRaisesRegex(PermissionError, "已存在"):
                writer.insert("uat.test_table", ["id"], [{"id": "ORIGINAL", "value": 9}])
            with self.assertRaisesRegex(PermissionError, "QA-owned"):
                writer.update("uat.test_table", ["id"], [{"id": "ORIGINAL"}], {"value": 9})
            with self.assertRaisesRegex(PermissionError, "QA-owned"):
                writer.delete("uat.test_table", ["id"], [{"id": "ORIGINAL"}])
        self.assertEqual(
            self.executor.rows["uat.test_table"][json.dumps({"id": "ORIGINAL"}, sort_keys=True)]["value"], 1
        )

    def test_create_scope_allows_insert_and_cleanup_but_not_direct_mutation(self):
        with patch.dict(os.environ, self.env(QA_WRITE_SCOPE="test_data_create"), clear=False):
            writer = GuardedUATWriter.from_env(self.executor)
            writer.insert("uat.test_table", ["id"], [{"id": "QA-1", "value": 1}])
            with self.assertRaises(PermissionError):
                writer.update("uat.test_table", ["id"], [{"id": "QA-1"}], {"value": 2})
            with self.assertRaises(PermissionError):
                writer.delete("uat.test_table", ["id"], [{"id": "QA-1"}])
            self.assertEqual(writer.cleanup(), 1)
            writer.assert_clean()
        self.assertNotIn(json.dumps({"id": "QA-1"}, sort_keys=True), self.executor.rows["uat.test_table"])

    def test_table_allowlist_and_plan_estimate_are_hard_gates(self):
        with patch.dict(os.environ, self.env(QA_ESTIMATED_WRITE_ROWS="1"), clear=False):
            writer = GuardedUATWriter.from_env(self.executor)
            with self.assertRaises(PermissionError):
                writer.insert("uat.other_table", ["id"], [{"id": "QA-1"}])
            with self.assertRaises(unittest.SkipTest):
                writer.insert("uat.test_table", ["id"], [{"id": "QA-1"}, {"id": "QA-2"}])

    def test_bulk_write_needs_scope_runtime_approval_and_approved_limit(self):
        rows = [{"id": f"QA-{i}", "value": i} for i in range(3)]
        base = self.env(QA_UAT_BULK_THRESHOLD="2", QA_ESTIMATED_WRITE_ROWS="3")
        with patch.dict(os.environ, base, clear=False):
            writer = GuardedUATWriter.from_env(self.executor)
            with self.assertRaisesRegex(unittest.SkipTest, "bulk_write"):
                writer.insert("uat.test_table", ["id"], rows)

        bulk = self.env(
            QA_WRITE_SCOPE="bulk_write",
            QA_UAT_BULK_THRESHOLD="2",
            QA_ESTIMATED_WRITE_ROWS="3",
            QA_UAT_BULK_APPROVAL_REF_EXPECTED="APPROVED-001",
            QA_UAT_BULK_APPROVED_MAX_ROWS="3",
        )
        with patch.dict(os.environ, bulk, clear=False):
            writer = GuardedUATWriter.from_env(self.executor)
            with self.assertRaisesRegex(unittest.SkipTest, "审批"):
                writer.insert("uat.test_table", ["id"], rows)

        bulk["QA_UAT_BULK_APPROVAL_REF"] = "APPROVED-001"
        with patch.dict(os.environ, bulk, clear=False):
            writer = GuardedUATWriter.from_env(self.executor)
            writer.insert("uat.test_table", ["id"], rows)
            writer.cleanup()
            writer.assert_clean()

    def test_write_kill_switch_and_non_uat_block_before_executor(self):
        with patch.dict(os.environ, self.env(QA_UAT_WRITE_ENABLED="0"), clear=False):
            with self.assertRaises(unittest.SkipTest):
                GuardedUATWriter.from_env(self.executor)
        with patch.dict(os.environ, self.env(QA_ENVIRONMENT="demo"), clear=False):
            with self.assertRaises(unittest.SkipTest):
                GuardedUATWriter.from_env(self.executor)

    def test_failed_insert_reservation_can_only_be_cleaned_as_qa_owned_identity(self):
        class PartialExecutor(FakeExecutor):
            def __call__(self, action):
                if action["operation"] == "insert":
                    row = action["rows"][0]
                    identity = {field: row[field] for field in action["key_fields"]}
                    self.rows[action["table"]][self.key(identity)] = dict(row)
                    raise RuntimeError("synthetic failure")
                return super().__call__(action)

        executor = PartialExecutor()
        with patch.dict(os.environ, self.env(), clear=False):
            writer = GuardedUATWriter.from_env(executor)
            with self.assertRaises(RuntimeError):
                writer.insert("uat.test_table", ["id"], [{"id": "QA-PARTIAL", "value": 1}])
            self.assertEqual(writer.cleanup(), 1)
            writer.assert_clean()
        self.assertIn(json.dumps({"id": "ORIGINAL"}, sort_keys=True), executor.rows["uat.test_table"])
        self.assertNotIn(json.dumps({"id": "QA-PARTIAL"}, sort_keys=True), executor.rows["uat.test_table"])


if __name__ == "__main__":
    unittest.main()
