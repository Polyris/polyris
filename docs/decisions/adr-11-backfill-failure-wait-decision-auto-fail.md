# ADR #11 — Backfill Failure: Wait for Decision (not auto-fail)

**Decision:** When a task fails during backfill, suppress Slack/PagerDuty but still wait for user decision via UI.

**Rationale:**
- Auto-fail gave no way to restart/skip from UI
- Slack/PD noise during backfill is unwanted (dozens of dates)
- User still needs control over failed tasks
- 5h timeout same as normal runs — consistent behavior

**Implementation:** `Check_Is_Backfill` → `Wait_For_Decision` (skipping Interactive_Slack and Send_PagerDuty_Alert). Task shows `waiting_decision` in UI with skip/fail/restart buttons.
