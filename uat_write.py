"""Backward-compatible import surface for controlled UAT writes.

New code should import from qa_core.uat_writer. This module remains so existing
project tests do not break during the core/profile refactor.
"""
from qa_core.uat_writer import GuardedUATWriter, TestDataRegistry, WRITE_SCOPES

__all__ = ["GuardedUATWriter", "TestDataRegistry", "WRITE_SCOPES"]
