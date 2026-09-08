from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


KNOWLEDGE_TYPES = {
    "module", "requirement", "business_rule", "database", "database_table",
    "database_field", "api", "testcase", "bug", "sql",
}
KNOWLEDGE_STATUSES = {
    "draft", "review_required", "confirmed", "stale", "conflict", "deprecated",
}


@dataclass
class KnowledgeSource:
    source_id: str
    document_source_id: str
    type: str
    page_id: str
    title: str
    version: int
    updated_at: str
    section: str
    hash: str


@dataclass
class KnowledgeEntity:
    id: str
    type: str
    name: str
    system: str
    module: list[str]
    status: str = "draft"
    confidence: str = "medium"
    aliases: list[str] = field(default_factory=list)
    claims: dict = field(default_factory=dict)
    sources: list[KnowledgeSource] = field(default_factory=list)
    first_seen: str = field(default_factory=lambda: date.today().isoformat())
    last_seen: str = field(default_factory=lambda: date.today().isoformat())


def validate_status(status: str) -> str:
    if status not in KNOWLEDGE_STATUSES:
        raise ValueError(f"未知知识状态：{status}")
    return status
