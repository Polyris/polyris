# ADR #118 — `polyris-deploy --all` / `--only` — bulk deploy and destroy across directories

*Amended by ADR #139: which files and folders `--all` loads, a process per folder, and a `dag_id` two files make refused.*

**Context.** `polyris-deploy` only ever handled one file (default `dag.py`) per invocation
— deploying N example/pipeline directories meant N manual `cd && polyris-deploy` calls,
with no way to batch them, no way to skip a bad one and keep going, and no summary of what
actually happened. This became acute once the example set grew past a handful of
directories (`examples_temp/01_single_task` through `15_assets_every_event`) and needed
redeploying together after a rename sweep.

**Decision.** Two new mutually-exclusive flags: `--all` (every immediate subdirectory of
the current directory) and `--only DIR [DIR ...]` (just the listed ones). Both apply the
same discovery rule per directory: scan **every** `.py` file in it (not only `dag.py`; since ADR #139, only those whose code imports `DAG`, `dag` or `Pipeline` from polyris, and `dag.py`) and
deploy **every** DAG object found in each — a directory may hold more than one independent
pipeline file, and a file with zero DAGs (a shared `config.py`, a `utils.py`) is silently
skipped, not an error. This reuses `_load_dag_from_file`'s existing multi-DAG-per-file
scanning verbatim; nothing new was invented for "how many DAGs can one file hold."

`--select DAG_ID` (pre-existing, for picking one DAG out of a multi-DAG file) composes with
bulk mode: applied independently inside each directory, so a directory that doesn't contain
a DAG with that ID is skipped rather than counted as a failure.

**Naming.** The obvious name for "which folders" was `--select`, but that flag already means
"which DAG, by ID, within one file" and is shared with `polyris-output`, documented in three
places (`CLI.md` ×2, `DEPLOY.md`). Renaming the established flag to free up the name would
be the actual breaking change here, not adding a new one — `--only` was chosen instead,
leaving `--select`'s existing meaning untouched. `--dirs` was considered and rejected as
less natural to read at the call site than `--only`.

**Failure isolation.** One DAG failing must not stop the batch — the whole point of bulk
mode is walking away with a complete picture, not a `sys.exit` on directory #2 of 15. Rather
than refactor every internal `sys.exit(1)` path inside `deploy_pipeline` (validation
failures, AWS errors, and more) into a raised exception, each per-DAG call in the bulk loop
is wrapped in its own `try/except SystemExit`, converting an exit-with-nonzero-code into a
recorded failure and moving on. This is deliberately the least invasive option: it changes
nothing about `deploy_pipeline`'s internals or its single-file callers, and it composes with
any *future* internal exit path the same way, without needing to remember to wrap it.

**Sequential, not parallel — for now.** Deploys run one at a time. Considered and set aside:
concurrent deploys would interleave output from multiple pipelines, need explicit
concurrency limits (CloudFormation and the AWS API throttle), and add real complexity for a
15-pipeline batch that finishes in minutes sequentially. Revisit if bulk batches grow large
enough that sequential time becomes the actual bottleneck.

**Consequences.**
- `--all`/`--only` are mutually exclusive with each other and with `--file` (bulk mode
  discovers files itself — passing both is a usage error, not a silent override).
- `--file`'s CLI default changed internally from the literal `"dag.py"` to `None`, so
  `main()` can tell "not passed" from "passed," and reject `--file` when bulk mode is
  active. Single-file behavior (`polyris-deploy` with no flags) is unchanged; `Path(args.file
  or "dag.py")` reproduces the old default exactly.
- A final summary lists every DAG attempted with ✅/❌, and the process exits non-zero if
  anything failed — safe to gate a CI job or script on.
- `polyris-output`/`polyris-validate` were not given the same bulk treatment — out of scope
  for this decision; revisit if the same need shows up there.
