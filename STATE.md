# STATE.md — where things stand

Twenty lines, not a changelog. What a session needs to know before touching
anything. `CHANGELOG.md` is the history; this is the present.

Update it when something moves. Paths here are checked by
`tests/sdk/test_context_terms.py`.

---

**Version:** 0.93.0 · **Next milestone:** first public OSS release

## In flight

- Nothing. The v0.93.1 gap-closing pass is complete and green.

## Decided, not done

- **`useBackfillsListQuery` belongs in EE.** Its last free caller became the paid
  `BackfillNavTab`, so by ADR #99's own rule it should live under
  `ui/src/ee/team/hooks/queries/`. Left in the free tree as dead code pointing at
  a route that 404s there. Moving it is a cross-repo contract change (#6).
- **The init wizard ignores an explicit "none"** for dependencies.
  `_build_custom_pipeline` cannot tell "never asked" from "declined", so the
  sequential-chain fallback overrides the answer and a fan-out of independent
  tasks is unreachable. Pinned by a test that documents current behaviour;
  fixing it changes that function's contract.
## Known debt, measured

- **Coverage omit list is still too wide.** `polyris/local.py` is ~20% AWS (only
  `_run_localstack` touches boto3); in `polyris/register.py` only
  `get_sfn_client` does. The rest is pure logic sitting unmeasured behind an
  "AWS/CLI" exemption. The fix shape is to split the AWS call out and measure the
  remainder — as was done for `polyris/output.py` and `polyris/init.py`.
- **npm audit: 3 high, 0 critical** — `next`, `postcss`, `sharp`. All unreachable
  under `output: 'export'`; reasoning recorded in `SECURITY.md`. No patched
  `next` 16.2.x exists.

## Before tagging

Principle #15 is not optional: deploy to dev and run a live smoke. The last pass
touched `sam/lambdas/console_api/routes/tasks.py` and
`sam/lambdas/console_api/task_actions.py` — DDB write paths where a mock can lie.
Exercise mark_success on a task with an outlet and confirm the asset event lands.
