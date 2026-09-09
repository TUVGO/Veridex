# Test Data Governance

The generic core separates test data into three classes:

- **REFERENCE**: existing UAT data used as read-only input/anchor. It may be queried and joined, but never UPDATEd or DELETEd by the QA workflow.
- **OWNED**: rows created by the current QA run/case. They may be mutated and must be cleaned up.
- **DERIVED**: rows produced by the system because of a QA action. They are not automatically considered owned; a project adapter must prove correlation before they can be adopted/cleaned.

## Reference data

Plans use aliases instead of storing real account/order/customer identifiers:

```json
"reference_data": [
  {
    "alias": "ACCOUNT_FULL_HISTORY",
    "entity": "account",
    "access": "readonly",
    "purpose": "Use an existing account with complete linked records"
  }
]
```

The real alias-to-value mapping belongs in a local/private profile such as
`profiles-private/<project>/reference-data.json` and must not be committed.

This lets a test use a deeply-linked existing reference entity without granting ownership of that entity or any existing row linked to it.

## Ownership

Ownership is record-level, not business-key-domain-level.

A QA-created record may reference an existing account or order, but the new record is owned only through its unique identity recorded by `TestDataRegistry`. The referenced entity itself remains REFERENCE.

Never delete by a broad condition such as `WHERE account_id = ...` when that could match historical rows. Cleanup should target the exact identities registered by the run.

## Derived rows

Examples include records created by a service/job/MQ consumer after the test triggers
a workflow. A future project adapter may adopt a DERIVED row only after proving a
correlation (for example request_id/run marker + time window + pre-check that the
identity did not exist). The generic core intentionally does not guess this.
