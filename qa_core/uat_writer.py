"""Controlled UAT test-data writes.

This module never accepts raw SQL. Project adapters provide an executor for the small,
structured action contract. Original/existing rows are read-only: INSERT first proves
the QA identity does not already exist, and UPDATE/DELETE only accept identities
registered by the current QA run/case.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import unittest


WRITE_SCOPES = ("readonly", "test_data_create", "test_data_mutate", "bulk_write")


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def payload_hash(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def identifier(value, *, qualified=False):
    pattern = r"[A-Za-z_][A-Za-z0-9_]*"
    if qualified:
        pattern += r"(?:\.[A-Za-z_][A-Za-z0-9_]*){0,2}"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError("数据库标识符不合法")
    return value


def _identity(identity_value, key_fields):
    if not isinstance(identity_value, dict):
        raise ValueError("记录 identity 必须是对象")
    key_fields = tuple(identifier(field) for field in key_fields)
    if not key_fields or len(set(key_fields)) != len(key_fields):
        raise ValueError("key_fields 不能为空或重复")
    if set(identity_value) != set(key_fields):
        raise ValueError("identity 必须且只能包含 key_fields")
    result = {}
    for field in key_fields:
        value = identity_value[field]
        if value is None or isinstance(value, (dict, list, tuple, set)):
            raise ValueError("ownership key 必须是非空标量")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("ownership key 不能是 NaN/Infinity")
        result[field] = value
    return result


def _identity_key(table, key_fields, identity_value):
    normalized = _identity(identity_value, key_fields)
    return canonical({"table": table, "key_fields": list(key_fields), "identity": normalized})


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


class TestDataRegistry:
    def __init__(self, path, run_id, case_id):
        self.path = Path(path)
        self.run_id = run_id
        self.case_id = case_id
        if self.path.is_file():
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if data.get("schema_version") != 1 or data.get("run_id") != run_id or data.get("case_id") != case_id:
                raise ValueError("Test Data Registry 与当前 run/case 不匹配")
            if not isinstance(data.get("records"), list):
                raise ValueError("Test Data Registry 结构无效")
        else:
            data = {"schema_version": 1, "run_id": run_id, "case_id": case_id, "records": []}
            _write_json(self.path, data)

    def _load(self):
        return json.loads(self.path.read_text(encoding="utf-8-sig"))

    def _save(self, data):
        _write_json(self.path, data)

    def reserve(self, table, key_fields, identities):
        table = identifier(table, qualified=True)
        key_fields = tuple(key_fields)
        normalized = [_identity(item, key_fields) for item in identities]
        data = self._load()
        for item in normalized:
            key = _identity_key(table, key_fields, item)
            existing = next((r for r in reversed(data["records"]) if r.get("ownership_key") == key), None)
            if existing and existing.get("status") != "deleted":
                raise PermissionError("ownership identity 已被当前测试占用")
            generation = int(existing.get("generation", 0)) + 1 if existing else 1
            data["records"].append({
                "table": table,
                "key_fields": list(key_fields),
                "identity": item,
                "ownership_key": key,
                "generation": generation,
                "status": "reserved",
                "reserved_at": now(),
            })
        self._save(data)

    def _transition(self, table, key_fields, identities, allowed, target):
        table = identifier(table, qualified=True)
        key_fields = tuple(key_fields)
        normalized = [_identity(item, key_fields) for item in identities]
        data = self._load()
        for item in normalized:
            key = _identity_key(table, key_fields, item)
            record = next((r for r in reversed(data["records"]) if r.get("ownership_key") == key), None)
            if not record or record.get("status") not in allowed:
                raise PermissionError("记录不属于当前 QA run/case，拒绝修改或删除")
            record["status"] = target
            record[f"{target}_at"] = now()
        self._save(data)

    def activate(self, table, key_fields, identities):
        self._transition(table, key_fields, identities, {"reserved"}, "active")

    def assert_owned(self, table, key_fields, identities, *, allow_reserved=False):
        allowed = {"active", "reserved"} if allow_reserved else {"active"}
        table = identifier(table, qualified=True)
        key_fields = tuple(key_fields)
        data = self._load()
        for item in identities:
            key = _identity_key(table, key_fields, item)
            record = next((r for r in reversed(data["records"]) if r.get("ownership_key") == key), None)
            if not record or record.get("status") not in allowed:
                raise PermissionError("目标记录不是当前 QA-owned 数据")

    def mark_deleted(self, table, key_fields, identities):
        self._transition(table, key_fields, identities, {"active", "reserved"}, "deleted")

    def unfinished_groups(self):
        data = self._load()
        groups = {}
        for record in data["records"]:
            if record.get("status") == "deleted":
                continue
            key = (record["table"], tuple(record["key_fields"]))
            groups.setdefault(key, []).append(record["identity"])
        return groups

    def unfinished_count(self):
        return sum(len(items) for items in self.unfinished_groups().values())


class GuardedUATWriter:
    """Policy gate around a project-specific structured DB executor."""

    def __init__(
        self,
        executor,
        *,
        run_id,
        case_id,
        scope,
        registry,
        audit_path,
        writable_tables,
        bulk_threshold,
        estimated_rows,
        approval_ref_expected=None,
        approval_ref_provided=None,
        approved_max_rows=None,
    ):
        if scope not in WRITE_SCOPES or scope == "readonly":
            raise unittest.SkipTest("当前用例没有 UAT 写权限")
        self.executor = executor
        self.run_id = run_id
        self.case_id = case_id
        self.scope = scope
        self.registry = registry
        self.audit_path = Path(audit_path)
        self.writable_tables = {identifier(item, qualified=True) for item in writable_tables}
        if not self.writable_tables:
            raise unittest.SkipTest("未配置 QA_UAT_WRITABLE_TABLES")
        if type(bulk_threshold) is not int or bulk_threshold < 1:
            raise unittest.SkipTest("QA_UAT_BULK_THRESHOLD 必须是正整数")
        if type(estimated_rows) is not int or estimated_rows < 1:
            raise unittest.SkipTest("QA_ESTIMATED_WRITE_ROWS 必须是正整数")
        self.bulk_threshold = bulk_threshold
        self.estimated_rows = estimated_rows
        self.approval_ref_expected = approval_ref_expected
        self.approval_ref_provided = approval_ref_provided
        self.approved_max_rows = approved_max_rows

    @classmethod
    def from_env(cls, executor):
        if os.environ.get("QA_ENVIRONMENT") != "uat":
            raise unittest.SkipTest("写适配器仅允许 UAT")
        if os.environ.get("QA_UAT_WRITE_ENABLED") != "1":
            raise unittest.SkipTest("UAT 写入总开关未开启")
        run_id = os.environ.get("QA_RUN_ID")
        case_id = os.environ.get("QA_CASE_ID")
        scope = os.environ.get("QA_WRITE_SCOPE", "readonly")
        registry_path = os.environ.get("QA_TEST_DATA_REGISTRY")
        audit_path = os.environ.get("QA_UAT_WRITE_AUDIT_PATH")
        if not all((run_id, case_id, registry_path, audit_path)):
            raise unittest.SkipTest("缺少 QA run/case 写入审计环境变量")
        tables = [item.strip() for item in os.environ.get("QA_UAT_WRITABLE_TABLES", "").split(",") if item.strip()]
        try:
            threshold = int(os.environ.get("QA_UAT_BULK_THRESHOLD", ""))
            estimated = int(os.environ.get("QA_ESTIMATED_WRITE_ROWS", ""))
        except ValueError as exc:
            raise unittest.SkipTest("写入行数配置无效") from exc
        maximum_text = os.environ.get("QA_UAT_BULK_APPROVED_MAX_ROWS")
        maximum = int(maximum_text) if maximum_text and maximum_text.isdigit() else None
        registry = TestDataRegistry(registry_path, run_id, case_id)
        return cls(
            executor,
            run_id=run_id,
            case_id=case_id,
            scope=scope,
            registry=registry,
            audit_path=audit_path,
            writable_tables=tables,
            bulk_threshold=threshold,
            estimated_rows=estimated,
            approval_ref_expected=os.environ.get("QA_UAT_BULK_APPROVAL_REF_EXPECTED"),
            approval_ref_provided=os.environ.get("QA_UAT_BULK_APPROVAL_REF"),
            approved_max_rows=maximum,
        )

    def _table(self, table):
        table = identifier(table, qualified=True)
        if table not in self.writable_tables:
            raise PermissionError("目标表不在 QA_UAT_WRITABLE_TABLES 白名单")
        return table

    def _check_count(self, operation, table, count):
        if type(count) is not int or count < 1:
            raise ValueError("写操作记录数必须为正整数")
        if count > self.estimated_rows:
            raise unittest.SkipTest(
                f"{operation} {table} 实际 {count} 行超过计划 estimated_rows={self.estimated_rows}"
            )
        if count <= self.bulk_threshold:
            return
        if self.scope != "bulk_write":
            raise unittest.SkipTest(
                f"{operation} {table} 预计 {count} 行超过批量阈值 {self.bulk_threshold}，需要 bulk_write 审批"
            )
        if (
            not self.approval_ref_expected
            or self.approval_ref_provided != self.approval_ref_expected
            or type(self.approved_max_rows) is not int
            or count > self.approved_max_rows
        ):
            raise unittest.SkipTest("批量写入审批引用缺失/不匹配或批准行数不足")

    def _audit(self, status, operation, table, count, action, **extra):
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        item = {
            "time": now(),
            "run_id": self.run_id,
            "case_id": self.case_id,
            "status": status,
            "operation": operation,
            "table": table,
            "row_count": count,
            "payload_sha256": payload_hash(action),
        }
        item.update(extra)
        with self.audit_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(item, ensure_ascii=False, allow_nan=False) + "\n")

    @staticmethod
    def _affected(result):
        if type(result) is int:
            return result
        if isinstance(result, dict) and type(result.get("affected_rows")) is int:
            return result["affected_rows"]
        raise RuntimeError("写 executor 必须返回 affected_rows")

    @staticmethod
    def _returned_identities(result, key_fields):
        if isinstance(result, dict):
            result = result.get("identities")
        if not isinstance(result, list):
            raise RuntimeError("exists/matches executor 必须返回 identities")
        return [_identity(item, key_fields) for item in result]

    def _query_identities(self, operation, table, key_fields, identities, expected=None):
        action = {
            "operation": operation,
            "table": table,
            "key_fields": list(key_fields),
            "identities": identities,
        }
        if expected is not None:
            action["expected"] = expected
        return self._returned_identities(self.executor(action), key_fields)

    @staticmethod
    def _identity_set(items):
        return {canonical(item) for item in items}

    def insert(self, table, key_fields, rows):
        if self.scope not in ("test_data_create", "test_data_mutate", "bulk_write"):
            raise PermissionError("当前 write_scope 不允许 INSERT")
        table = self._table(table)
        if not isinstance(rows, list) or not rows or any(not isinstance(row, dict) for row in rows):
            raise ValueError("rows 必须是非空对象数组")
        key_fields = tuple(identifier(field) for field in key_fields)
        identities = [_identity({field: row.get(field) for field in key_fields}, key_fields) for row in rows]
        self._check_count("insert", table, len(rows))

        preexisting = self._query_identities("exists", table, key_fields, identities)
        if preexisting:
            raise PermissionError("INSERT identity 已存在；拒绝覆盖或接管原始/既有数据")

        self.registry.reserve(table, key_fields, identities)
        action = {"operation": "insert", "table": table, "key_fields": list(key_fields), "rows": rows}
        self._audit("started", "insert", table, len(rows), action)
        try:
            affected = self._affected(self.executor(action))
            existing = self._query_identities("exists", table, key_fields, identities)
            if affected != len(rows) or self._identity_set(existing) != self._identity_set(identities):
                raise RuntimeError("INSERT 影响行数或写后存在性校验不一致")
            self.registry.activate(table, key_fields, identities)
            self._audit("success", "insert", table, len(rows), action, affected_rows=affected)
            return affected
        except Exception as exc:
            self._audit("failed", "insert", table, len(rows), action, error_type=type(exc).__name__)
            raise

    def update(self, table, key_fields, identities, changes):
        if self.scope not in ("test_data_mutate", "bulk_write"):
            raise PermissionError("当前 write_scope 不允许 UPDATE")
        table = self._table(table)
        key_fields = tuple(identifier(field) for field in key_fields)
        identities = [_identity(item, key_fields) for item in identities]
        if not isinstance(changes, dict) or not changes:
            raise ValueError("changes 必须是非空对象")
        if any(field in changes for field in key_fields):
            raise PermissionError("不允许修改 ownership key")
        for field in changes:
            identifier(field)
        self.registry.assert_owned(table, key_fields, identities)
        self._check_count("update", table, len(identities))
        action = {
            "operation": "update", "table": table, "key_fields": list(key_fields),
            "identities": identities, "changes": changes,
        }
        self._audit("started", "update", table, len(identities), action)
        try:
            affected = self._affected(self.executor(action))
            matched = self._query_identities("matches", table, key_fields, identities, changes)
            if affected != len(identities) or self._identity_set(matched) != self._identity_set(identities):
                raise RuntimeError("UPDATE 影响行数或写后值校验不一致")
            self._audit("success", "update", table, len(identities), action, affected_rows=affected)
            return affected
        except Exception as exc:
            self._audit("failed", "update", table, len(identities), action, error_type=type(exc).__name__)
            raise

    def delete(self, table, key_fields, identities):
        if self.scope not in ("test_data_mutate", "bulk_write"):
            raise PermissionError("当前 write_scope 不允许直接 DELETE；test_data_create 只能 cleanup")
        table = self._table(table)
        key_fields = tuple(identifier(field) for field in key_fields)
        identities = [_identity(item, key_fields) for item in identities]
        self.registry.assert_owned(table, key_fields, identities)
        self._check_count("delete", table, len(identities))
        action = {"operation": "delete", "table": table, "key_fields": list(key_fields), "identities": identities}
        self._audit("started", "delete", table, len(identities), action)
        try:
            affected = self._affected(self.executor(action))
            remaining = self._query_identities("exists", table, key_fields, identities)
            if affected != len(identities) or remaining:
                raise RuntimeError("DELETE 影响行数或删除后存在性校验不一致")
            self.registry.mark_deleted(table, key_fields, identities)
            self._audit("success", "delete", table, len(identities), action, affected_rows=affected)
            return affected
        except Exception as exc:
            self._audit("failed", "delete", table, len(identities), action, error_type=type(exc).__name__)
            raise

    def cleanup(self):
        groups = self.registry.unfinished_groups()
        cleaned = 0
        for (table, key_fields), identities in groups.items():
            table = self._table(table)
            self._check_count("cleanup", table, len(identities))
            self.registry.assert_owned(table, key_fields, identities, allow_reserved=True)
            action = {"operation": "delete", "table": table, "key_fields": list(key_fields), "identities": identities}
            self._audit("started", "cleanup", table, len(identities), action)
            try:
                affected = self._affected(self.executor(action))
                remaining = self._query_identities("exists", table, key_fields, identities)
                if remaining:
                    raise RuntimeError("cleanup 后仍存在 QA-owned 数据")
                self.registry.mark_deleted(table, key_fields, identities)
                self._audit("success", "cleanup", table, len(identities), action, affected_rows=affected)
                cleaned += len(identities)
            except Exception as exc:
                self._audit("failed", "cleanup", table, len(identities), action, error_type=type(exc).__name__)
                raise
        return cleaned

    def assert_clean(self):
        remaining = self.registry.unfinished_count()
        if remaining:
            raise AssertionError(f"仍有 {remaining} 条 QA-owned 测试数据未清理")
