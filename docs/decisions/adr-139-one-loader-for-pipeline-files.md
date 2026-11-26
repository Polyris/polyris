---
area: sdk
accepted: by 2026-11-18
---
# ADR #139 — One loader runs a pipeline file for every command

**Context.** Until 1.2.2, `polyris-validate`, `polyris-deploy`, `polyris-output` and `polyris-local` each ran a pipeline file with a loader of their own and found its DAGs by a rule of their own, so a file could pass validate and then fail to deploy — `with DAG("x"):` without `as` (backlog F18) was the first of several such files. `--all` also had two rules for which files to load: validate searched every folder and matched files by name, deploy loaded every `.py` one folder down, Glue scripts included. Patching each case failed review after review (Principle #42), so every rule below rests on a fact of the code or of Python, not on a guess.

**Decision.**

1. **One loader.** `polyris.loader.open_pipeline(path)` — and `load_pipeline(path)`, the same closed on return — is the only way a command runs a pipeline file. The path is made absolute; while the file runs, its folder and then the folder above come first on `sys.path`, so a sibling `config.py` wins over the project root's; `sys.exit(0)` ends it normally; an error, a non-zero exit or one `dag_id` made twice is a `PipelineLoadError`. The file runs as module `polyris_pipeline`.
2. **A DAG belongs to the file unless an `import` made it.** `recording_dags` records each DAG when it is made (`DAG.__post_init__`). It is the file's unless Python was importing a module between the DAG and the loader (`importlib._bootstrap._find_and_load` on the stack). So `with` or not, `@dag`, a factory, `exec` and a file run with `exec_module` count; a DAG a module the file imports makes does not, nor one whose `with` block raised an error.
3. **Which files `--all` loads.** `polyris-validate --all` and `polyris-deploy --all` load the folders directly in the run directory (`pipeline_folders`; hidden folders and caches aside), and in each, `dag.py` and every `.py` whose code imports `DAG`, `dag` or `Pipeline` from polyris (`is_pipeline_file`: the file is parsed from its bytes with `ast`, never run; test files are not pipelines). A file that does not parse is loaded when its text imports a DAG maker, so its error is reported. Both commands warn about pipeline files in the run directory itself or deeper (`pipelines_out_of_reach`): what validates is what deploys.
4. **A process per folder.** Each folder's files load in a fresh Python process (`polyris.folder_process`: `python -P -m polyris.folder_process REQUEST ANSWER`, two JSON files), so nothing one folder imports — its `settings.py`, a path it adds, a library's import-time side effects — reaches the next. The child imports the same polyris as the parent; a stopped parent stops it; one that ends without answering fails its folder — in `deploy --all`, also when it was collecting the folder's `dag_id`s, so a folder never skips the check below.
5. **One `dag_id`, one file.** Each DAG is one stack. A file that makes a `dag_id` twice does not load; two files or folders that make one fail `validate --all`, and `deploy --all` / `destroy --all` collect every folder's `dag_id`s first and then deploy nothing.

**Alternatives rejected.**
- Recording DAGs at `with` exit, or reading the module's globals: misses a DAG made without `with` or without a name.
- Owning a DAG by the nearest module frame: a file run with `exec_module`, or a copy a helper imports back, looks like another module.
- Isolating folders in one process by forgetting the modules a file imported: every layout (a symlink, a `src/`, an editable library) needed another exception.
- A process per file: the files of one folder share their sibling modules (`settings.py`) as a project, and it would start twice as many processes.
- Choosing files by name or by text, as Airflow's safe mode does ("airflow" and "dag"): skipped real pipelines, and ran task scripts whose comments or `POLYRIS_PIPELINE_NAME` matched; Airflow pairs it with an ignore file each user maintains.
- Keeping the last DAG made for a `dag_id`, as Airflow does: deploys silently replace each other. Refused, as in Dagster.

**Consequences.**
- Breaking (CHANGELOG): `--all` skips a `.py` whose code does not import a DAG maker from polyris (a pipeline built through a project wrapper: name it `dag.py` or use `--file`); `validate --all` no longer loads pipeline files in the run directory or deeper than one folder; `--all` exits 1 when a pipeline file does not load; a `dag_id` made twice, in one file or across files and folders, is refused.
- `--all` takes about a quarter of a second more per folder, and `deploy --all` loads each file twice (the `dag_id` check, then the deploy).
- A DAG made in a worker thread counts for the file being loaded; no command loads files from several threads.
- Code: `polyris/loader.py` and `polyris/folder_process.py`; every command loads through them. Tests: `tests/sdk/test_loader.py` (with the check that nothing else runs a pipeline file), `test_folder_process.py`, `test_validation_core.py`, `test_deploy_bulk.py`. Rule in `polyris/CLAUDE.md`.
