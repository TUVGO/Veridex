from __future__ import annotations

import os


WRITE_SCOPES = ("readonly", "test_data_create", "test_data_mutate", "bulk_write")


def _required_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} 必须是非空字符串")


def validate_write_policy(case, environment):
    """Validate the generic per-case write contract.

    Missing policy is deliberately readonly for backward compatibility.
    """
    policy = case.get("write_policy")
    if policy is None:
        return {"scope": "readonly", "estimated_rows": 0, "cleanup_required": False}
    if not isinstance(policy, dict):
        raise ValueError("write_policy 必须是对象")
    scope = policy.get("scope", "readonly")
    if scope not in WRITE_SCOPES:
        raise ValueError("write_policy.scope 不合法")
    if scope == "readonly":
        return {"scope": "readonly", "estimated_rows": 0, "cleanup_required": False}
    if environment != "uat":
        raise ValueError("数据库写用例仅允许 environment=uat")
    estimated = policy.get("estimated_rows")
    if type(estimated) is not int or estimated < 1:
        raise ValueError("写用例必须声明正整数 write_policy.estimated_rows")
    if policy.get("cleanup_required") is not True:
        raise ValueError("写用例必须声明 cleanup_required=true")
    approval = policy.get("bulk_approval")
    if scope != "bulk_write" and approval is not None:
        raise ValueError("仅 bulk_write 可声明 bulk_approval")
    if approval is not None:
        if not isinstance(approval, dict):
            raise ValueError("bulk_approval 必须是对象")
        _required_text(approval.get("approval_ref"), "bulk_approval.approval_ref")
        maximum = approval.get("approved_max_rows")
        if type(maximum) is not int or maximum < estimated:
            raise ValueError("approved_max_rows 必须为不小于 estimated_rows 的正整数")
    return {
        "scope": scope,
        "estimated_rows": estimated,
        "cleanup_required": True,
        "bulk_approval": approval,
    }


def bulk_approval_ready(policy, environ=None):
    environ = os.environ if environ is None else environ
    approval = policy.get("bulk_approval")
    return bool(
        approval
        and environ.get("QA_UAT_BULK_APPROVAL_REF")
        and environ.get("QA_UAT_BULK_APPROVAL_REF") == approval.get("approval_ref")
    )
