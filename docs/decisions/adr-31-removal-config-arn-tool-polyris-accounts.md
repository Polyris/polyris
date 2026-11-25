# ADR #31 — Removal of `config.arn()` and `[tool.polyris.accounts]`

**Context:** `config.arn()` constructed Step Function ARNs from account IDs in `pyproject.toml`.

**Decision:** Hardcode the full ARN with the `STAGE` variable directly in the pipeline:
```python
arn=f"arn:aws:states:us-east-1:ACCOUNT_ID:stateMachine:myorg-{STAGE}-extract"
```

**Reasons:**
- Transparency: it is immediately clear what is being called
- Fewer abstractions — `pyproject.toml` becomes minimal
- `accounts` duplicated information that already lives in Terraform

**Consequences:** `config.arn()`, `config.accounts`, `_ConfigSection` — removed.
