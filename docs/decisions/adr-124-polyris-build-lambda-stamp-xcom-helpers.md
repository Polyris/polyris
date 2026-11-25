# ADR #124 — `polyris-build-lambda`: stamp XCom helpers into Lambda handler directories

**Context.** Lambda functions that participate in xcom (`xcom.push`, `xcom.get`, `xcom.pull`) need the `polyris.xcom` module available at runtime. The standard path is `pip install polyris` via a `requirements.txt` in the Lambda directory, which works but requires a network-accessible PyPI or a VPC endpoint. Teams building Lambda zip packages in air-gapped or locked-down environments, or that want to minimise the zip size to the absolute minimum (xcom.py is self-contained — only stdlib + boto3, which is already in the Lambda runtime), prefer to vendor just the helper.

**Decision.** Add `polyris-build-lambda <dir>` — a CLI command that copies `polyris/xcom.py` from the installed polyris wheel into `<dir>/polyris/xcom.py`, alongside a minimal `<dir>/polyris/__init__.py` that re-exports the module. The result is a self-contained `polyris/` package that supports `from polyris import xcom` without any pip install step.

**Why stamp, not pip?**
- `polyris/xcom.py` has zero relative imports. Its only dependencies are stdlib (`json`, `decimal`, `typing`, `boto3.exceptions`) and boto3, which is pre-installed in every Lambda runtime. The entire module is self-contained — no sub-package, no assets, no C extension.
- Stamping produces a deterministic artifact from the installed wheel. The zip build is reproducible without network access.
- `requirements.txt` remains supported as Option B (documented in `DATA_PASSING.md`). Users who already have a pip build step use that; `polyris-build-lambda` is for those who don't.

**Scope.** Only `xcom.py` is stamped — not the full SDK. Lambda functions need xcom helpers, not the DSL or deploy tooling. If other modules become useful in Lambda contexts in the future, a separate scoped command should be evaluated per the same criterion: is the module self-contained (no polyris-internal relative imports)?

**Drift tradeoff.** The stamped copy is a snapshot of the installed version. Re-running `polyris-build-lambda --force <dir>` after a polyris upgrade is the user's responsibility. `polyris-build-lambda` prints the stamped version on success so a CI step can assert it. This is advisory: there is no mechanical version guard in the generated files. Teams that want one can grep the stamped `xcom.py` for the version comment in their CI.

**Relation to ADR #102** (Lambda packaging via `requirements.txt`). ADR #102 governs how the console Lambda (the backend API) packages the SDK. It is unrelated to how end-user task Lambda functions get their xcom helpers. The console Lambda continues to use `requirements.txt`; end-user task Lambdas can use either option.

**Consequences.**
- New console script `polyris-build-lambda` in `pyproject.toml`.
- `polyris/build_lambda.py` is measured at 100% coverage (no boto3 calls, not in the coverage omit list).
- If the installed polyris is a compiled-only wheel (no `.py` source), `polyris-build-lambda` raises `FileNotFoundError` with a clear message rather than silently stamping bytecode.
