# Phase 1A.2 Reuse Survey — Structured & Multimodal Confluence

## Decision

Phase 1A.2 stops expanding the in-house Confluence parser as the primary path. The recommended ingestion path is:

```text
Confluence Server/Data Center
  -> confluence-markdown-exporter (CME)
  -> QA Brain Source Package
  -> Docling for referenced attachments
  -> Codex extraction tasks
  -> Knowledge Merge / Conflict / Review
```

The existing minimal REST collector remains a fallback and synthetic-test fixture.

| Capability | Upstream | License | Reviewed Commit | Decision |
|---|---|---|---|---|
| Page + descendant export | Spenhouet/confluence-markdown-exporter 5.4.0 | MIT | 74dc982b008321005e8ccecceb79d7e9a5fe2b8d | INTEGRATE |
| Tables / links / images / attachments / macros | Spenhouet/confluence-markdown-exporter | MIT | 74dc982b008321005e8ccecceb79d7e9a5fe2b8d | INTEGRATE |
| draw.io / PlantUML preservation | Spenhouet/confluence-markdown-exporter | MIT | 74dc982b008321005e8ccecceb79d7e9a5fe2b8d | INTEGRATE |
| PDF/DOCX/PPTX/XLSX/image parsing | docling-project/docling | MIT | 3d330388b8f37291964a613274fd24b357edcc3b | INTEGRATE |
| QA knowledge/version/conflict | QA Brain | project code | current | BUILD |

No third-party source code is copied. Integrations call installed upstream CLIs through narrow adapters.

## Security boundary

- Third-party CLIs run with `shell=False`.
- QA Brain does not forward `QA_*` credentials into subprocesses.
- CME credentials remain in CME's local config, not Git or command arguments.
- HTTP Confluence requires both explicit `--allow-http` and `--allow-insecure-auth`.
- Docling is invoked locally without `--enable-remote-services`.
- Real exports, normalized sources, parsed attachments and review queues remain gitignored.
