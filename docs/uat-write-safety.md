# Controlled UAT Write Policy

## Confirmed environment rule

UAT allows CRUD, with these safety boundaries:

- Existing/original UAT data: SELECT only. Never UPDATE or DELETE.
- QA-created data: may be INSERTed, UPDATEd and DELETEd by the same QA run/case.
- Bulk writes: require explicit human review before execution.
- Production remains outside this write path.

## Plan contract

Existing plans remain compatible and default to `readonly`.

A write case declares:

```json
"write_policy": {
  "scope": "test_data_mutate",
  "estimated_rows": 5,
  "cleanup_required": true
}
```

Scopes:

- `readonly`: no write.
- `test_data_create`: INSERT QA-owned rows; direct UPDATE/DELETE is blocked, but cleanup is allowed.
- `test_data_mutate`: INSERT/UPDATE/DELETE QA-owned rows.
- `bulk_write`: same as mutate, but large operations require approval.

A bulk case may only run after the plan records the reviewed limit:

```json
"write_policy": {
  "scope": "bulk_write",
  "estimated_rows": 100000,
  "cleanup_required": true,
  "bulk_approval": {
    "approval_ref": "APPROVED-20260908-001",
    "approved_max_rows": 100000
  }
}
```

At runtime the operator must also set `QA_UAT_BULK_APPROVAL_REF` to the same approval reference. This is intentionally a second gate.

## Runtime gates

Write cases require:

- `QA_UAT_WRITE_ENABLED=1`
- `QA_UAT_WRITABLE_TABLES`: exact comma-separated table allowlist
- `QA_UAT_BULK_THRESHOLD`: project-defined positive integer threshold

The workflow injects:

- `QA_RUN_ID`
- `QA_CASE_ID`
- `QA_WRITE_SCOPE`
- `QA_ESTIMATED_WRITE_ROWS`
- `QA_TEST_DATA_REGISTRY`
- `QA_UAT_WRITE_AUDIT_PATH`
- bulk approval expectations when applicable

## Ownership rule

`uat_write.GuardedUATWriter` does not accept raw SQL.

Before INSERT it asks the project executor whether every proposed ownership identity already exists. If any identity exists, the INSERT is rejected. This prevents the test from claiming existing/original data.

Every new identity is written to `test-data-registry.json`. UPDATE and DELETE only accept identities that are active in that same run/case registry.

The executor contract is structured:

- `exists`
- `insert`
- `update`
- `matches`
- `delete`

Project-specific adapters translate these actions into parameterized database operations.

## Cleanup gate

Write cases must clean all QA-owned records before a PASS is accepted.

The workflow checks both:

- `test-data-registry.json`
- `write-audit.jsonl`

A child unittest may return PASS, but the workflow changes it to BLOCKED if:

- the registry is missing;
- write audit is missing;
- there was no successful write operation;
- any QA-owned record remains `reserved` or `active`.

If a write fails after a key was reserved, cleanup may still remove that reserved identity because the adapter proved it did not exist before the attempted INSERT.

## Bulk approval

`QA_UAT_BULK_THRESHOLD` is not hard-coded in the repository because the user has not defined the company/project threshold.

When an actual operation exceeds the configured threshold:

1. scope must be `bulk_write`;
2. plan must contain `bulk_approval.approval_ref`;
3. runtime `QA_UAT_BULK_APPROVAL_REF` must match;
4. actual rows must not exceed `approved_max_rows`;
5. actual rows must not exceed the plan's `estimated_rows`.

## Remaining trust boundary

The framework can strongly enforce these rules for database writes that go through `GuardedUATWriter`.

It cannot technically stop arbitrary Python test code from opening a separate database connection with a privileged credential and bypassing the adapter. For hard organizational enforcement, use a dedicated UAT QA credential or DB proxy whose write permissions are limited to approved test tables/partitions. The Skill/AGENTS rules forbid bypassing the adapter.
