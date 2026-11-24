# Web Console (UI)

The console shows your pipelines, their runs and their tasks, and lets you act on them: start a run, stop or pause it, and skip, fail, mark successful, stop or restart a task. `polyris-up` installs it and prints its URL. Building or changing the console itself: [DEVELOPMENT.md → Console](../tools/DEVELOPMENT.md#console-ui).

## Contents

- [Views](#views)
- [Pipelines sidebar](#pipelines-sidebar)
- [Pipeline page](#pipeline-page)
- [Task modal](#task-modal)
- [Actions](#actions)
- [History: runs and tasks](#history-runs-and-tasks)
- [Notifications](#notifications)
- [Auto-refresh](#auto-refresh)
- [Help and keyboard shortcuts](#help-and-keyboard-shortcuts)
- [Browser support](#browser-support)

---

## Views

| View | Description |
|------|-------------|
| **Pipelines** | The sidebar of pipelines and the open pipeline's graph |
| **History** | Runs and tasks across pipelines, with a **Runs** / **Tasks** toggle |

---

## Pipelines sidebar

The sidebar lists every registered pipeline, one line each. It reads the runs of the last 14 days:
- **Status icon**: the latest run's status (success, failed, running, aborted). A running run that waits for you shows a question mark (a decision) or a pause sign. A pipeline with no run in those 14 days shows a small hollow circle. A run is running until every task has finished; hover to see how many tasks failed.
- **Pipeline name**: hover for the full name and schedule. **redeploy** next to it means its runs fail until you run `polyris-deploy` for it ([TROUBLESHOOTING.md](TROUBLESHOOTING.md#runs-fail-at-once-redeploy-needed)).
- **Run sparkline**: up to the last 10 runs, oldest on the left, coloured by status; the newest run is always the rightmost bar. Hover it for the runs spelled out.
- **Schedule**: see the table below; hidden on tablets.

**Needs attention.** Pipelines whose latest run failed, waits for your decision, or is still running with a failed task are also listed on top. A paused or aborted run does not put a pipeline there: a stop is not a failure. Click the header to fold the section.

**Groups.** Set `group=` on the `DAG` to list a pipeline under that group ([DSL reference](../features/DSL.md#dag-parameters)); case does not matter. Pipelines without a group go under **Ungrouped**, at the bottom; when no pipeline sets a group, the list is flat. Click a group to fold it, or use the button next to the count to fold or unfold all of them; this browser remembers folded groups. A folded group still shows how many of its pipelines need attention. A group you add shows after the pipeline's next deploy or run.

**Search.** Type to filter by pipeline name or group; every word must match. Enter opens the first match. The command palette (`⌘K`) matches pipeline names the same way.

**Refresh.** A refresh never moves rows under your pointer; they reorder once you leave the list. If a refresh fails, a line above the list says how old the statuses are, and the **System** pill in the header says why.

| Schedule | Shown as |
|------------|---------|
| `cron(0 8 * * ? *)` | `daily @ 08:00` |
| `cron(0 10 ? * MON *)` | `Mon @ 10:00` |
| `rate(6 hours)` | `every 6h` |
| `@daily`, `@hourly`, … (presets) | `daily`, `hourly`, … |
| Anything else | as written, cut to fit the row |
| `schedule=[asset]` | `on` and the asset, e.g. `on raw_orders`; with several, `on 3 assets` (hover for them, e.g. `on (a & b) \| c`) |
| No schedule | `manual` |

---

## Pipeline page

The open pipeline's graph, for the run you pick (the date picker and the run selector), with the run's actions. Drag to pan, scroll to zoom, click a task to open its modal. The lock button lets you drag tasks.

| Task status | Colour |
|--------|-------|
| waiting | Grey |
| running | Blue (animated) |
| success | Green |
| failed | Red |
| skipped | Orange |
| stopped | Grey |
| waiting_paused | Yellow |

---

## Task modal

Click a task to open it.

**Details**
- **Duration**, **Status**, **Dependencies** (how many upstream tasks), and **Attempt**: ↻ N counts your Restarts, ⭮ N/M which automatic retry this is, of how many. Attempt is hidden when the task was never restarted or retried.
- **Why it waits**: for a waiting task, the reasons polyris recorded and since when, the deadline of its wait or decision, when its `wait_before` countdown ends, and what its trigger rule makes of its upstream tasks.
- **Error**: why the task failed.
- **Execution Name**, **Pipeline Execution**: the task's and the run's names, as the API takes them.
- **Child Pipeline**: for a task that runs another pipeline, a link to it.
- **Started**, **Finished**.
- **Asset Dependencies**: the assets it waits for with `wait_for`.
- **Trigger Rule**: shown when it is not `all_success`.
- **AWS Console**: links to the task's executions in Step Functions.
- **Task Type**: `sfn`, `lambda`, `glue`, …

**History**: the task's timeline: each decision about it, its starts, automatic retries, its end and any operator's action, with their times.

**Input / Output**: the variables and upstream outputs the task read, and its stored output.

**Actions**: see below.

---

## Actions

**Task actions**, in the task modal's Actions tab:

| Action | What it does |
|--------|-------------|
| **Skip Task** | Marks the task skipped: its dependants run as after a success |
| **Mark Failed** | Marks it failed: dependants that need its success end `upstream_failed`; the rest of the run goes on |
| **Mark Successful** | Marks it successful and records new data for its outlets: its dependants start |
| **Stop Task** | Stops an unfinished task and its work: it ends `stopped`, which its dependants' rules count as a failure |
| **Restart Task** | Runs a finished or undecided task again; tick *Also rerun the dependants its failure or stop decided* to rerun them too |
| **Run to Here** | Starts a new run, for the date on view, of this task and its upstream tasks; the run skips the others |
| **Run from Here** | Starts a new run, for the date on view, of this task and its downstream tasks |
| **Run Only This** | Starts a new run, for the date on view, of this task alone |

**Run actions**, on the pipeline page:

| Action | What it does |
|--------|-------------|
| **Run** | Starts a run now. The dialog takes the run's input as JSON, `{"current_date": "2026-01-15"}` by default with today's date; change `current_date` to run another date, and add keys to pass variables |
| **Pause** | Pauses the run: running tasks finish, no task starts. The pause lasts until you resume |
| **Resume** | Resumes the paused run: the tasks it held start |
| **Stop** | Stops the run: its unfinished tasks stop, dependants first, within seconds |

While a Stop is in progress the page says so and offers no action on the run.

---

## History: runs and tasks

Both lists load newest first, one page at a time. The footer says whether everything is loaded: **`50+ runs`** while older rows exist, **`73 runs`** once they are all there. **Show older runs / tasks** loads the next page; the `‹ ›` pager walks the rows already loaded, ten at a time.

Without a date, both reach the runs started in the last 90 days. A date reaches every run of that date. With a rare status, a page of **Tasks** can come back short or empty while **Show older** still reads further.

**Runs**: one row per run. A run is running until every task has finished; while it runs, its status says how many tasks failed (`running, 2 failed`). Filters: pipeline, status, and the run's date.

**Tasks**: one row per task. Filters: pipeline, status, date. The status cell shows ↻ for Restarts and ⭮ for automatic retries; the **Source** column shows what started the run: Console, Schedule or Asset.

---

## Notifications

The bell lists, whatever the date of the run:
- tasks that failed in the last 48 hours, one per run;
- tasks waiting for your decision, the nearest deadline first, with when each fails unless you decide;
- polyris's warnings, named by their kind ([notifications.md](../features/notifications.md)).

Click a notification to open its task or pipeline.

---

## Auto-refresh

The console refreshes while the **Auto** toggle in the header is on: every 30 seconds, and every 15 seconds while a task of the open run counts down its `wait_before`. Click the toggle to pause it.

---

## Help and keyboard shortcuts

Press **?** for the Help modal: every keyboard shortcut, grouped by where it applies, and every status icon. The most used:

| Key | Action |
|-----|--------|
| `?` | Open help |
| `⌘K` | Command palette |
| `Esc` | Close modal |
| `⌘R` | Refresh |
| `/` | Focus the pipeline search (Pipelines page); `Enter` there opens the first match, `↓` moves into the list |
| `J` / `K` | Next / previous pipeline or group, while the sidebar list has focus; `Esc` goes back to the search |

---

## Browser support

The latest Chrome, Firefox, Safari and Edge.
