from __future__ import annotations

import json
from pathlib import Path


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_evidence_summary(case_dir, run_id, case_id):
    """Turn write registry/audit files into a conservative PASS gate."""
    case_dir = Path(case_dir)
    registry_path = case_dir / "test-data-registry.json"
    audit_path = case_dir / "write-audit.jsonl"
    summary = {
        "valid": False,
        "registry": registry_path.name,
        "audit": audit_path.name,
        "owned_records": 0,
        "unfinished_records": 0,
        "successful_writes": 0,
        "reason": "",
    }
    if not registry_path.is_file():
        summary["reason"] = "写用例缺少 Test Data Registry"
        return summary
    if not audit_path.is_file():
        summary["reason"] = "写用例缺少 Write Audit"
        return summary
    try:
        registry = _read_json(registry_path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        summary["reason"] = "Test Data Registry 无效"
        return summary
    if registry.get("schema_version") != 1 or registry.get("run_id") != run_id or registry.get("case_id") != case_id:
        summary["reason"] = "Test Data Registry 与当前批次/用例不匹配"
        return summary
    records = registry.get("records")
    if not isinstance(records, list):
        summary["reason"] = "Test Data Registry 结构无效"
        return summary
    summary["owned_records"] = len(records)
    summary["unfinished_records"] = sum(
        not isinstance(record, dict) or record.get("status") != "deleted" for record in records
    )
    try:
        audits = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        summary["reason"] = "Write Audit 无效"
        return summary
    summary["successful_writes"] = sum(
        isinstance(item, dict)
        and item.get("status") == "success"
        and item.get("operation") in ("insert", "update", "delete", "cleanup")
        for item in audits
    )
    if summary["successful_writes"] < 1:
        summary["reason"] = "写用例没有成功写操作证据"
        return summary
    if summary["unfinished_records"]:
        summary["reason"] = f"仍有 {summary['unfinished_records']} 条 QA-owned 数据未清理"
        return summary
    summary["valid"] = True
    summary["reason"] = "QA-owned 数据已完成写入审计和清理"
    return summary
