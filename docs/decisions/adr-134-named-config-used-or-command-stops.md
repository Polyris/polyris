---
area: deploy
accepted: by 2026-11-14
---
# ADR #134 — A config the user names is used, or the command stops

**Context.** `polyris-deploy` takes its stage settings, the AWS profile among them, from `config.py`: the file `POLYRIS_CONFIG` names, or the first `config.py` with `ENVIRONMENTS` from the working directory up to the project root. Every way of failing to use that file ended in "no config": a `POLYRIS_CONFIG` naming a missing file, a file without `ENVIRONMENTS`, one that did not parse or failed when run. Without a config the deploy uses the default AWS credentials and the platform it finds by tag, so it can act in another account. The 1.2.0 rehearsal hit the class twice (Principle #42): the stage view read the environments before anything had loaded them (F5), and a `POLYRIS_CONFIG` left pointing at a deleted file was dropped without a word (F17). Both times the deploy went to the default account and stopped only because that account refused it.

**Decision.**

1. A config the user pointed at loads, or the command stops with the reason: `POLYRIS_CONFIG` naming a missing file, a file without `ENVIRONMENTS` or one that does not parse; a `config.py` on the walk that does not parse; any `config.py` that fails when run. `polyris.config` raises `ConfigError`; `polyris-deploy` prints it and exits 1 before any AWS call.
2. The deploy target names the config file (`none` without one) and the AWS account the credentials reach, before the first write.
3. No config is still a valid choice: without `POLYRIS_CONFIG` and without a `config.py`, the deploy uses the defaults, and the target says so.

**Key design choices.**

*Stop rather than warn.* A warning scrolls past and the deploy goes on in the wrong account; an exit costs one rerun.

*A broken `config.py` on the walk stops too.* Skipping it would go on with a parent's config or none, the same silent change of target.

**Consequences.**
- Tests: `test_config.py` (each way a named config fails), `test_deploy_summary_and_conflicts.py` (the target lines; a missing `POLYRIS_CONFIG` stops `polyris-deploy` before `boto3.Session`).
- Rule in `polyris/CLAUDE.md`.
