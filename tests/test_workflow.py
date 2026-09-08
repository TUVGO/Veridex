"""真实子进程验证执行、证据完整性和保守结论；所有测试产物写入临时目录。"""
import copy
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid

from workflow import build_report, digest, read_json, run_plan, validate, write_json


CHILD_TESTS = '''import time
import unittest
from pathlib import Path
from qa_evidence import equal

class Checks(unittest.TestCase):
    def test_pass(self):
        equal(self, "count", 3, 3)

    def test_fail(self):
        equal(self, "count", 2, 3)

    def test_empty(self):
        pass

    @unittest.skip("environment unavailable")
    def test_skip(self):
        equal(self, "count", 3, 3)

    def test_slow(self):
        time.sleep(5)
        equal(self, "count", 3, 3)

    def test_sentinel(self):
        Path("EXECUTED").write_text("unexpected", encoding="utf-8")
        equal(self, "count", 3, 3)

    def test_write_leak(self):
        import json
        import os
        registry = {
            "schema_version": 1,
            "run_id": os.environ["QA_RUN_ID"],
            "case_id": os.environ["QA_CASE_ID"],
            "records": [{"status": "active"}],
        }
        Path(os.environ["QA_TEST_DATA_REGISTRY"]).write_text(json.dumps(registry), encoding="utf-8")
        Path(os.environ["QA_UAT_WRITE_AUDIT_PATH"]).write_text(
            json.dumps({"status": "success", "operation": "insert"}) + "\\n", encoding="utf-8"
        )
        equal(self, "count", 3, 3)
'''


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        # Windows 沙盒下 tempfile 的 0700 目录可能不继承所需 ACL；普通目录仅含合成数据。
        test_root = Path(__file__).resolve().parents[1] / "work" / "qa-tests"
        self.root = test_root / uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.assertEqual(self.root.resolve().parent, test_root.resolve())
        self.addCleanup(shutil.rmtree, self.root)
        self.source = self.root / "requirement.md"
        self.source.write_text("数量应为 3。", encoding="utf-8")
        (self.root / "business_checks.py").write_text(CHILD_TESTS, encoding="utf-8")
        self.plan = {
            "schema_version": 1, "project": "isolated-test", "environment": "demo",
            "requirement_version": "1", "system_version": "fixture-1",
            "source": {"path": str(self.source), "sha256": digest(self.source),
                       "review_status": "confirmed", "notes": "synthetic"},
            "requirements": [{"id": "R1", "statement": "数量应为 3", "source_ref": "line 1",
                              "rule_type": "presence", "confirmed": True}],
            "cases": [self.case("C1", "pass")],
            "execution": {"cwd": str(self.root), "timeout_seconds": 10}, "notes": []}

    @staticmethod
    def case(identifier, method, mode="automated"):
        return {"id": identifier, "title": method, "requirement_id": "R1", "jira_key": "DEMO-170",
                "priority": "P1", "module": "fixture", "tags": [], "preconditions": "fixture ready",
                "steps": ["inspect count"], "test_data": "synthetic", "expected": "count=3",
                "rule_ids": ["R1"], "mode": mode,
                "test": f"business_checks.Checks.test_{method}" if mode == "automated" else None,
                "reason": "manual review" if mode != "automated" else ""}

    def run_fixture(self):
        path = self.root / "plan.json"
        write_json(path, self.plan)
        folder = run_plan(path, self.root / "runs")
        return folder, read_json(folder / "results.json")

    def test_actual_subprocess_pass_records_checks(self):
        folder, result = self.run_fixture()
        record = result["cases"][0]
        self.assertEqual(record["status"], "PASS")
        self.assertEqual(record["checks"][0]["actual"], 3)
        self.assertTrue(record["checks"][0]["matched"])
        self.assertTrue((folder / "evidence/C1/stdout.log").exists())
        self.assertIn("本次计划用例全部通过", (folder / "report.md").read_text(encoding="utf-8"))

    def test_failure_empty_and_skip_are_not_pass(self):
        self.plan["cases"] = [self.case("C1", "fail"), self.case("C2", "empty"), self.case("C3", "skip")]
        folder, result = self.run_fixture()
        self.assertEqual([r["status"] for r in result["cases"]], ["FAIL", "BLOCKED", "BLOCKED"])
        self.assertFalse(result["cases"][0]["checks"][0]["matched"])
        self.assertIn("C1", (folder / "defects.md").read_text(encoding="utf-8"))

    def test_timeout_is_blocked(self):
        self.plan["cases"] = [self.case("C1", "slow")]
        self.plan["execution"]["timeout_seconds"] = 0.2
        _, result = self.run_fixture()
        self.assertEqual(result["cases"][0]["status"], "BLOCKED")
        self.assertIn("超时", result["cases"][0]["reason"])

    def test_changed_source_rejects_before_execution(self):
        self.plan["cases"] = [self.case("C1", "sentinel")]
        self.source.write_text("changed requirement", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "已变更"):
            self.run_fixture()
        self.assertFalse((self.root / "EXECUTED").exists())
        self.assertFalse((self.root / "runs").exists())

    def test_stale_source_does_not_execute(self):
        self.plan["cases"] = [self.case("C1", "sentinel")]
        self.plan["source"]["review_status"] = "stale"
        _, result = self.run_fixture()
        self.assertEqual(result["cases"][0]["status"], "BLOCKED")
        self.assertFalse((self.root / "EXECUTED").exists())

    def test_missing_or_changed_evidence_rejects_report(self):
        for mutation in ("delete", "tamper"):
            with self.subTest(mutation=mutation):
                folder, _ = self.run_fixture()
                evidence = folder / "evidence/C1/checks.jsonl"
                if mutation == "delete":
                    evidence.unlink()
                else:
                    evidence.write_text("{}\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "证据缺失或被修改"):
                    build_report(folder)

    def test_duplicate_case_and_invalid_jira_reject(self):
        duplicate = copy.deepcopy(self.plan)
        duplicate["cases"].append(copy.deepcopy(duplicate["cases"][0]))
        with self.assertRaisesRegex(ValueError, "用例编号重复"):
            validate(duplicate)
        for jira in ("DEMO-170 DEMO-171", "需求 DEMO-170", ""):
            with self.subTest(jira=jira):
                invalid = copy.deepcopy(self.plan)
                invalid["cases"][0]["jira_key"] = jira
                with self.assertRaises(ValueError):
                    validate(invalid)

    def test_status_counts_include_unexecuted_cases(self):
        self.plan["cases"] = [self.case("C1", "pass"), self.case("C2", "fail"),
                              self.case("C3", "empty"), self.case("C4", "review", "manual")]
        folder, _ = self.run_fixture()
        self.assertEqual(dict(build_report(folder)), {"PASS": 1, "FAIL": 1, "BLOCKED": 1, "NOT_RUN": 1})
        report = (folder / "report.md").read_text(encoding="utf-8")
        self.assertIn("2/4 = 50.0%", report)
        self.assertIn("PASS/(PASS+FAIL) = 50.0%", report)
        self.assertIn("尚不具备完整通过结论", report)

    def test_write_case_pass_is_blocked_when_qa_owned_data_is_not_cleaned(self):
        self.plan["environment"] = "uat"
        case = self.case("C1", "write_leak")
        case["write_policy"] = {"scope": "test_data_mutate", "estimated_rows": 1, "cleanup_required": True}
        self.plan["cases"] = [case]
        with patch.dict(__import__("os").environ, {"QA_UAT_WRITE_ENABLED": "1"}, clear=False):
            _, result = self.run_fixture()
        self.assertEqual(result["cases"][0]["status"], "BLOCKED")
        self.assertIn("未清理", result["cases"][0]["reason"])

    def test_declared_write_case_without_registry_cannot_pass(self):
        self.plan["environment"] = "uat"
        case = self.case("C1", "pass")
        case["write_policy"] = {"scope": "test_data_create", "estimated_rows": 1, "cleanup_required": True}
        self.plan["cases"] = [case]
        with patch.dict(__import__("os").environ, {"QA_UAT_WRITE_ENABLED": "1"}, clear=False):
            _, result = self.run_fixture()
        self.assertEqual(result["cases"][0]["status"], "BLOCKED")
        self.assertIn("Registry", result["cases"][0]["reason"])

    def test_bulk_write_without_explicit_approval_never_executes(self):
        self.plan["environment"] = "uat"
        case = self.case("C1", "sentinel")
        case["write_policy"] = {"scope": "bulk_write", "estimated_rows": 1000, "cleanup_required": True}
        self.plan["cases"] = [case]
        with patch.dict(__import__("os").environ, {"QA_UAT_WRITE_ENABLED": "1"}, clear=False):
            _, result = self.run_fixture()
        self.assertEqual(result["cases"][0]["status"], "BLOCKED")
        self.assertIn("审批", result["cases"][0]["reason"])
        self.assertFalse((self.root / "EXECUTED").exists())

    def test_write_policy_rejects_demo_and_requires_cleanup(self):
        invalid = copy.deepcopy(self.plan)
        invalid["cases"][0]["write_policy"] = {
            "scope": "test_data_mutate", "estimated_rows": 1, "cleanup_required": True
        }
        with self.assertRaisesRegex(ValueError, "environment=uat"):
            validate(invalid)
        invalid["environment"] = "uat"
        invalid["cases"][0]["write_policy"]["cleanup_required"] = False
        with self.assertRaisesRegex(ValueError, "cleanup_required"):
            validate(invalid)

    def test_uncovered_rule_prevents_complete_pass_conclusion(self):
        self.plan["requirements"].append({"id": "R2", "statement": "另一个规则", "source_ref": "line 2",
                                           "rule_type": "business_filter", "confirmed": True})
        folder, result = self.run_fixture()
        self.assertEqual(result["cases"][0]["status"], "PASS")
        report = (folder / "report.md").read_text(encoding="utf-8")
        self.assertIn("未覆盖", report)
        self.assertIn("尚不具备完整通过结论", report)
        self.assertNotIn("本次计划用例全部通过", report)


if __name__ == "__main__":
    unittest.main()
