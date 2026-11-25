# ADR #66 — Export CSV removed from BackfillDetailPage (v0.78.7)

#### Context

BackfillDetailPage shipped with an "Export CSV" button (v0.78.0) that
downloaded a four-column CSV: `partition_key`, `status`, `child_count`,
`child_execution_ids`. The implementation was ~30 lines of code in
`handleExportCsv`: build the rows, escape commas/quotes, create a Blob,
click an anchor tag, revoke the URL.

User feedback (Mike, v0.78.5 deploy review): *"цей експорт не містить
корисної інформації, можемо прибрати"*.

#### Decision

Removed the button, the `handleExportCsv` function, and the `Download`
icon import. The replaced test asserts the button is no longer
rendered, which pins the removal so a future PR can't silently put it
back.

#### Why remove rather than improve

The four columns described what a backfill already shows visually in
the partition heatmap (`partition_key`, `status`) and the children
table (`child_count`, `child_execution_ids`). Anything the CSV
provided was already on-screen. The audit workflows the user actually
runs (debugging a failed partition, reviewing what got backfilled)
don't benefit from a separate text file.

A useful CSV export would need to surface NEW information not visible
in the UI — execution durations, task-level breakdowns, error
classifications, cost data. That's a bigger feature, not a tweak.
When that need arrives (likely as part of Pro-tier cost tracking or
SLA reporting), the export is best designed against the actual
workflow rather than patched onto the current button.

#### Recovery path

If a user requests CSV export later, the implementation pattern is
documented in the v0.78.0 commit history (search for `handleExportCsv`).
Restoring it is mechanical. The harder work — designing a CSV that's
worth downloading — is the part this ADR defers, not the code.

#### Lesson

Public-facing feature removals deserve a brief ADR even when they're
"obvious wins". The ADR captures (a) what the feature was, (b) why
removing was better than improving, (c) where to look if it needs to
come back. Without this, the next person seeing "v0.78.0 had CSV
export, v0.78.7 doesn't" has to dig through git log to understand
intent.
