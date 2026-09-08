# Contributing to Veridex

Thanks for your interest in Veridex.

## Principles

- Keep core behavior generic.
- Put system-specific behavior behind adapters or configuration.
- Preserve conservative PASS / FAIL / BLOCKED semantics.
- Add tests for every new deterministic gate.
- Never commit real credentials, private endpoints, customer identifiers, or organization data.

## Development

```bash
python -m venv .venv
pip install -e .
python -m unittest discover -s tests -v
```

Open an issue before large architectural changes so the public/private integration boundary stays clear.
