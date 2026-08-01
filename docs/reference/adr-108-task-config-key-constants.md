# ADR #108 — `TaskConfigKey`: shared constants for the SDK↔wrapper `task_config` contract

> **Status:** ACCEPTED — implemented. No wire-format change: the str-Enum
> members serialize to the exact same JSON keys the wrapper already reads.
> Structural follow-up to ADR #106 (parameter parity), closing the bug class
> that produced the original `emr` Step drift.

## Context

The per-task `task_config` dict is a two-sided contract: the SDK writer
(`generators._build_task_branch` and the step-wrapper path) produces keys, and
the `run_task` wrapper template reads them as `$states.input.task_config.<key>`
JSONata references. Both sides used **bare string literals** with no shared
declaration. Nothing structural prevented a writer key and a reader key from
drifting apart — which is exactly how the `emr` Step passthrough broke before
ADR #106: the writer emitted a key the template never read.

ADR #106 added behavior tests that resolve writer output through the real
template, which catches drift *for parameters that the tests exercise*. It did
not give the keys a single home, so every new key was still born as two
uncoordinated literals.

## Decision

1. **Single source of truth:** `polyris.constants.TaskConfigKey`, a
   `str`-based `Enum` with one member per contract key (per-type payload keys
   plus the ADR #107 retry-policy keys). `str`-Enum members compare equal to
   their plain string values and `json.dumps` emits the plain value, so ASL
   output and snapshots are byte-identical.
2. **Writer uses the enum:** both `task_config` builder sites in
   `generators.py` key their dicts exclusively via `TaskConfigKey` members.
3. **Contract test pins both sides**
   (`tests/sdk/test_task_config_contract.py`):
   - *Reader side:* every `task_config.<key>` reference regex-extracted from
     `sfn.tpl.json` must be a declared enum value.
   - *Writer side:* for each task type with **all** parameters set, the
     produced key set must equal the expectation **derived from the enum** —
     a literal-string key on the writer side surfaces as an unexpected key.
   - *Source guard:* no `task_config["..."]` literal writes remain in
     `generators.py`.
   - The deliberately-empty `sfn` contract (ADR #106) stays pinned.

## Consequences

- Adding a contract key = add an enum member first; tests fail on either side
  until both writer and template agree. Silent drift is no longer possible.
- The wrapper template itself cannot import Python constants (it is JSON), so
  the template side is enforced by test rather than by construction. That is
  the strongest guarantee available without a template code-generator, which
  would be a much larger change than this contract warrants today.
- Enum members flowing through `json.dumps` and dict equality behave as plain
  strings; no downstream consumer sees a difference.
