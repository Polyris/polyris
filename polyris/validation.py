"""
Pipeline Validation Module

Provides cross-pipeline validation including:
- Asset cycle detection between DAGs
- Asset schedule validation
- Orphaned asset detection

Usage:
    from polyris.validation import validate_all
    
    results = validate_all('./pipelines')
    if results['errors']:
        for err in results['errors']:
            print(f"ERROR: {err}")
        sys.exit(1)
"""

import io
import sys
from contextlib import nullcontext, redirect_stdout
from typing import IO, Dict, List, Set, Tuple, Optional, Any
from dataclasses import asdict, dataclass, field
from polyris._ansi import c as _c, colorize_parser, BOLD, BOLD_GREEN, CYAN, RED, YELLOW
from polyris.platform import refused_task_role, reserved_machine, task_role_refusal
from polyris.resource_check import unresolved_message, unresolved_refs


@dataclass
class DAGInfo:
    """Extracted info from a DAG for validation."""
    dag_id: str
    file_path: str
    schedule: Optional[str] = None
    is_asset_triggered: bool = False
    trigger_assets: List[str] = field(default_factory=list)
    trigger_operator: str = 'OR'
    produced_assets: List[str] = field(default_factory=list)
    consumed_assets: List[str] = field(default_factory=list)
    # Maps asset name → list of column dicts (from column_to_dict). Captured
    # at DAG-load time when the outlet is a real Asset with a typed schema.
    # Used by `validate_schema_consistency` to surface cross-pipeline
    # conflicts before they hit the backend conflict resolver.
    outlet_schemas: Dict[str, List[Dict]] = field(default_factory=dict)


def _dag_infos(file_path: str, loaded_dags: list) -> List[DAGInfo]:
    """The DAG info of DAGs already loaded from ``file_path``."""
    dags = []
    for dag in loaded_dags:
        info = DAGInfo(
            dag_id=dag.dag_id,
            file_path=file_path,
            schedule=dag.schedule,
            is_asset_triggered=dag.is_asset_triggered
        )

        if dag.is_asset_triggered:
            schedule_info = dag.asset_schedule_info or {}  # set whenever is_asset_triggered
            info.trigger_assets = schedule_info.get('assets', [])
            info.trigger_operator = schedule_info.get('operator', 'OR')

        for task in dag.tasks:
            for outlet in getattr(task, 'outlets', []):
                asset_name = outlet.name if hasattr(outlet, 'name') else str(outlet)
                if asset_name not in info.produced_assets:
                    info.produced_assets.append(asset_name)
                # Capture the typed schema declared on this outlet so
                # cross-pipeline schema validation can compare them.
                # Schema lives on real `Asset` instances; refs / bare
                # strings have nothing to capture.
                outlet_schema = getattr(outlet, 'schema', None)
                if outlet_schema:
                    from .schema import column_to_dict
                    info.outlet_schemas[asset_name] = [
                        column_to_dict(c) for c in outlet_schema
                    ]

            for inlet in getattr(task, 'inlets', []):
                asset_name = inlet.name if hasattr(inlet, 'name') else str(inlet)
                if asset_name not in info.consumed_assets:
                    info.consumed_assets.append(asset_name)

        dags.append(info)

    return dags


def build_asset_graph(dags: List[DAGInfo]) -> Dict:
    """
    Build asset dependency graph from DAG info.
    
    Returns:
        {
            'producers': {asset_name: [dag_ids]},
            'consumers': {asset_name: [dag_ids]},  # via asset_schedule triggers
            'dag_produces': {dag_id: [asset_names]},
            'dag_consumes': {dag_id: [asset_names]}  # trigger assets
        }
    """
    graph: Dict[str, Dict[str, Any]] = {
        'producers': {},    # asset → list of DAGs that produce it
        'consumers': {},    # asset → list of DAGs triggered by it
        'dag_produces': {}, # DAG → list of assets it produces
        'dag_consumes': {}, # DAG → list of assets that trigger it
    }
    
    for dag in dags:
        dag_id = dag.dag_id
        
        # Track what this DAG produces
        graph['dag_produces'][dag_id] = dag.produced_assets
        for asset in dag.produced_assets:
            if asset not in graph['producers']:
                graph['producers'][asset] = []
            if dag_id not in graph['producers'][asset]:
                graph['producers'][asset].append(dag_id)
        
        # Track what triggers this DAG (asset_schedule)
        if dag.is_asset_triggered:
            graph['dag_consumes'][dag_id] = dag.trigger_assets
            for asset in dag.trigger_assets:
                if asset not in graph['consumers']:
                    graph['consumers'][asset] = []
                if dag_id not in graph['consumers'][asset]:
                    graph['consumers'][asset].append(dag_id)
    
    return graph


def detect_asset_cycles(graph: Dict) -> List[Dict]:
    """
    Detect cycles in the asset trigger graph.

    A cycle is a cross-pipeline infinite-trigger loop, e.g.::

        DAG_A produces asset_x -> triggers DAG_B
        DAG_B produces asset_y -> triggers DAG_A

    Self-loops (a DAG triggered by an asset it also produces) and longer
    multi-DAG loops (A -> B -> C -> A) are all reported.

    Args:
        graph: Asset graph from build_asset_graph()

    Returns:
        List of cycle info dicts with 'start_dag', 'path' and 'description'.
        Each distinct cycle is reported once (A->B->A and B->A->B are the
        same cycle).
    """
    # Build a DAG->DAG trigger adjacency: an edge dag --asset--> next_dag means
    # `dag` produces `asset`, which in turn triggers `next_dag`. A directed
    # cycle in this graph is the loop we are looking for.
    adjacency: Dict[str, List[Tuple[str, str]]] = {}
    for dag_id, produced in graph['dag_produces'].items():
        edges: List[Tuple[str, str]] = []
        for asset in produced:
            for next_dag in graph['consumers'].get(asset, []):
                edges.append((asset, next_dag))
        adjacency[dag_id] = edges

    cycles: List[Dict] = []
    seen_node_sets: set = set()

    # Only asset-triggered DAGs can sit in a trigger cycle (every node in such a
    # cycle has an incoming trigger edge), so they are the only valid starts.
    for start_dag in graph['dag_consumes'].keys():
        # Iterative DFS. Each frame carries the path of nodes and the asset
        # labels between them, plus the set of nodes already on this path so we
        # never loop forever on a *non-start* node. `start_dag` is deliberately
        # left reachable: an edge back to it is exactly the cycle we detect.
        stack: List[Tuple[str, List[str], List[str], Set[str]]] = [(start_dag, [start_dag], [], {start_dag})]
        while stack:
            current_dag, nodes, labels, on_path = stack.pop()
            for asset, next_dag in adjacency.get(current_dag, []):
                if next_dag == start_dag:
                    key = frozenset(nodes)
                    if key in seen_node_sets:
                        continue
                    seen_node_sets.add(key)
                    full_labels = labels + [asset]
                    path_repr: List[str] = []
                    for i, node in enumerate(nodes):
                        path_repr.append(node)
                        path_repr.append(f"→ {full_labels[i]} →")
                    path_repr.append(start_dag)
                    cycles.append({
                        'start_dag': start_dag,
                        'path': path_repr,
                        'description': ' '.join(path_repr),
                    })
                elif next_dag not in on_path:
                    stack.append((
                        next_dag,
                        nodes + [next_dag],
                        labels + [asset],
                        on_path | {next_dag},
                    ))

    return cycles


def validate_schema_consistency(all_dags: List[DAGInfo]) -> List[str]:
    """Cross-pipeline schema conflict detection.

    Surfaces warnings (not errors — the backend resolver still works) when
    the same asset is declared with conflicting schemas in 2+ pipelines.
    Specifically:

      1. **Type mismatch on same-name column**: column ``amount`` declared
         as ``decimal(10,2)`` in one pipeline and ``string`` in another.
         This is the most diagnosable case and the most likely to break
         downstream tooling — call it out by name.
      2. **Different column counts**: less serious, can be resolved by the
         richer-wins rule, but still worth surfacing for documentation
         hygiene.

    Single-pipeline issues (e.g. duplicate column names within one Asset)
    are caught by `normalize_schema` at DAG load time and never reach this
    function.

    Returns a list of human-readable warning strings, ordered first by
    asset name, then by column name within an asset.
    """
    warnings: List[str] = []

    # Group declarations by asset name → list of (dag_id, schema) pairs.
    by_asset: Dict[str, List[Tuple[str, List[Dict]]]] = {}
    for dag in all_dags:
        for asset_name, schema in dag.outlet_schemas.items():
            by_asset.setdefault(asset_name, []).append((dag.dag_id, schema))

    for asset_name in sorted(by_asset):
        decls = by_asset[asset_name]
        if len(decls) < 2:
            continue

        # 1. Type-mismatch detection on same-name columns. We accumulate
        #    (column_name → set of distinct (dag_id, type_str) pairs)
        #    and warn for any column with >1 distinct type.
        type_by_col: Dict[str, Dict[str, str]] = {}  # col → {dag_id: type_str}
        for dag_id, schema in decls:
            for col in schema:
                cname = col.get('name')
                ctype = col.get('type')
                if not cname or not ctype:
                    continue  # pragma: no cover -- a Column always carries a name and type; defensive skip
                type_by_col.setdefault(cname, {})[dag_id] = ctype

        for cname in sorted(type_by_col):
            seen = type_by_col[cname]
            distinct_types = set(seen.values())
            if len(distinct_types) > 1:
                # Order the dag→type pairs for stable output.
                pairs = ", ".join(
                    f"{dag_id!r}: {tp!r}" for dag_id, tp in sorted(seen.items())
                )
                warnings.append(
                    f"Asset '{asset_name}' has type conflict on column "
                    f"'{cname}' across pipelines — {pairs}"
                )

        # 2. Different column counts. Only emit when types are otherwise
        #    consistent (we already warned about type mismatches above).
        sizes = {dag_id: len(schema) for dag_id, schema in decls}
        if len(set(sizes.values())) > 1:
            shape = ", ".join(
                f"{dag_id!r}: {n} columns" for dag_id, n in sorted(sizes.items())
            )
            warnings.append(
                f"Asset '{asset_name}' declared with different column counts — "
                f"{shape}. Backend will pick the richest schema; consider "
                f"reconciling the declarations."
            )

    return warnings


class _Report(io.StringIO):
    """Captures the per-DAG checks' output in a folder's process, coloured as the parent's
    terminal would be: ``validate_all`` prints it after "Loaded N DAG(s)"."""

    def __init__(self, color: bool) -> None:
        super().__init__()
        self._color = color

    def isatty(self) -> bool:
        return self._color


def _collect_dag_info(files: List[str], *, check_asl: bool = False, verbose: bool = False,
                      report: Optional[IO[str]] = None) -> Tuple[List[DAGInfo], List[str]]:
    """Load each file once, in this process: its DAG info, and with ``check_asl`` its ASL,
    role and trigger-rule errors (their output goes to ``report`` when given). A file that does
    not load is an error."""
    from polyris.loader import load_pipeline

    infos: List[DAGInfo] = []
    errors: List[str] = []
    for file_path in files:
        try:
            dags = load_pipeline(file_path).dags
            file_infos = _dag_infos(file_path, dags)
            with redirect_stdout(report) if report is not None else nullcontext():
                valid, asl_errors = _validate_dags(dags, verbose) if check_asl else (True, [])
        except Exception as e:  # PipelineLoadError, or a bug of ours: this file's error, never the folder's crash
            errors.append(f"{file_path}: {e}")
            continue
        infos.extend(file_infos)
        if not valid:
            errors.append(f"{file_path}: {'; '.join(asl_errors) or 'ASL validation failed'}")
    return infos, errors


def _dag_info_as_json(info: DAGInfo) -> Dict[str, Any]:
    data = asdict(info)
    if not isinstance(info.schedule, str) and info.schedule is not None:
        data["schedule"] = str(info.schedule)  # an asset schedule; its assets are in trigger_assets
    return data


def collect_folder(files: List[str], check_asl: bool = False, verbose: bool = False,
                   color: bool = False) -> Dict[str, Any]:
    """A folder's process (``polyris.folder_process``, command ``collect``): its files' DAG
    info, errors and the per-DAG checks' output, as JSON for ``validate_all``."""
    import json

    report = _Report(color)
    infos, errors = _collect_dag_info(files, check_asl=check_asl, verbose=verbose, report=report)
    as_json = []
    for info in infos:
        try:
            as_json.append(json.loads(json.dumps(_dag_info_as_json(info))))
        except (TypeError, ValueError) as e:  # e.g. a schema default that is a date: this file's error
            errors.append(f"{info.file_path}: {e}")
    return {"infos": as_json, "errors": errors, "report": report.getvalue()}


def validate_all(directory: str = './pipelines', verbose: bool = True, check_asl: bool = False,
                 quiet: bool = False) -> Dict:
    """
    Validate all pipelines in directory.
    
    Checks:
    - Asset cycles between DAGs
    - Missing asset producers (optional warning)
    - One dag_id made by two files (error: each DAG is one stack)
    - With ``check_asl``: each DAG's ASL, roles and trigger rules (``_validate_dags``)

    Loads the pipeline files of each folder directly in ``directory``, once, in a Python
    process of the folder's own (ADR #139); a pipeline file in ``directory`` itself or deeper
    is a warning, as ``polyris-deploy --all`` would not deploy it.
    
    Args:
        directory: Root directory containing pipeline files
        verbose: Print progress
        check_asl: Also run the per-DAG checks, in each folder's process
        quiet: Hide what the pipelines print (``--json``); their errors still reach stderr
        
    Returns:
        {
            'pipelines': [DAGInfo],
            'errors': [str],
            'warnings': [str],
            'graph': asset_graph
        }
    """
    results: Dict[str, Any] = {
        'pipelines': [],
        'errors': [],
        'warnings': [],
        'graph': None
    }
    
    if verbose:
        print(f"{_c('Scanning', BOLD)} {_c(str(directory), CYAN)}...")

    from polyris import folder_process
    from polyris.loader import duplicate_dag_ids, files_in, out_of_reach_warnings, pipeline_folders

    folders = [(folder, [str(p) for p in files_in(folder).pipelines]) for folder in pipeline_folders(directory)]
    folders = [(folder, files) for folder, files in folders if files]
    results['warnings'].extend(out_of_reach_warnings(directory))

    if not folders:
        results['warnings'].append(f"No pipeline files found in {directory}")
        return results

    if verbose:
        print(f"{_c('Found', BOLD)} {sum(len(files) for _, files in folders)} pipeline file(s)")

    # Each folder runs in its own process (ADR #139): one folder's settings.py, a src/ it put on
    # the path, or a library's import-time side effects cannot reach the next folder's files.
    # A file that does not load (an error, a non-zero sys.exit) is an error, never a crash.
    all_dags: List[DAGInfo] = []
    reports: List[str] = []
    with folder_process.sigterm_stops_children():  # a stopped validate stops the folder's process too
        for folder, files in folders:
            answer = folder_process.run(
                "collect",
                {"files": files, "check_asl": check_asl, "verbose": verbose, "color": sys.stdout.isatty()},
                output="stderr" if quiet else "all",
            )
            if answer.error:
                results['errors'].append(f"{folder}: {answer.error}")
                continue
            dags = [DAGInfo(**d) for d in answer.payload["infos"]]
            results['errors'].extend(answer.payload["errors"])
            reports.append(answer.payload["report"])
            all_dags.extend(dags)
            results['pipelines'].extend(dags)
    results['errors'].extend(duplicate_dag_ids((d.dag_id, d.file_path) for d in all_dags))

    if verbose:
        print(f"{_c('Loaded', BOLD)} {len(all_dags)} DAG(s):")
        for dag in all_dags:
            trigger = dag.schedule if not dag.is_asset_triggered else f"asset:{dag.trigger_operator}"
            print(f"  • {_c(dag.dag_id, CYAN)} ({trigger})")
        print("".join(reports), end="")

    if not all_dags:
        # Surface load errors here — the verbose summary block below is never
        # reached when we return early, and main()'s elif not effective_verbose
        # branch is skipped when effective_verbose=True, so without this print
        # the user would see a silent exit 1 under --all -v with broken imports.
        if verbose and results['errors']:
            print(_c(f"\n❌ {len(results['errors'])} error(s) loading pipeline files:", RED))
            for err in results['errors']:
                print(f"   {err}")
        results['warnings'].append("No DAGs found in pipeline files")
        return results

    # Build asset graph
    if verbose:
        print(_c("\nBuilding asset graph...", BOLD))

    graph = build_asset_graph(all_dags)
    results['graph'] = graph

    # Detect cycles
    if verbose:
        print(_c("Checking for asset cycles...", BOLD))
    
    cycles = detect_asset_cycles(graph)
    
    for cycle in cycles:
        results['errors'].append(f"Cycle detected: {cycle['description']}")
    
    # Check for missing producers (warning only)
    for dag in all_dags:
        if dag.is_asset_triggered:
            for asset in dag.trigger_assets:
                if asset not in graph['producers']:
                    results['warnings'].append(
                        f"{dag.dag_id}: Trigger asset '{asset}' has no producer"
                    )

    # Schema-aware checks. Cross-pipeline schema conflicts are reported here
    # so the user sees them locally before deploy, instead of as a CW Logs
    # warning after backend conflict resolution silently picks a winner.
    schema_warnings = validate_schema_consistency(all_dags)
    results['warnings'].extend(schema_warnings)
    
    # Summary
    if verbose:
        print()
        if results['errors']:
            print(_c(f"❌ {len(results['errors'])} error(s) found:", RED))
            for err in results['errors']:
                print(f"   {err}")
        else:
            print(_c("✓ No cycles detected", BOLD_GREEN))

        if results['warnings']:
            print(_c(f"⚠️  {len(results['warnings'])} warning(s):", YELLOW))
            for warn in results['warnings']:
                print(f"   {warn}")
    
    return results


def _validate_single(dag_file: str, verbose: bool) -> Tuple[bool, List[str]]:
    """Validate a single pipeline file. Returns (valid, errors).

    Prints progress lines when verbose=True. Produces no stdout when
    verbose=False — the caller must pass verbose=False (via effective_verbose)
    when --json is active so that JSON consumers receive clean stdout.

    Loads the file through ``polyris.loader.load_pipeline``, the loader every CLI uses
    (ADR #139): the DAGs validated are the ones deploy would deploy.
    """
    from polyris.loader import PipelineLoadError, PipelineNotFoundError, load_pipeline

    try:
        dags = load_pipeline(dag_file).dags
    except PipelineNotFoundError:
        return False, [f"Pipeline file not found: {dag_file}"]
    except PipelineLoadError as e:
        return False, [f"Failed to load {dag_file}: {e}"]
    if not dags:
        return False, [f"No DAG found in {dag_file}"]
    return _validate_dags(dags, verbose)


def _validate_dags(dags: list, verbose: bool) -> Tuple[bool, List[str]]:
    """The ASL, role and trigger-rule checks of DAGs already loaded; (valid, errors)."""
    all_valid = True
    all_errors: List[str] = []
    for dag in dags:
        if verbose:
            print(f"{_c('Validating:', BOLD)} {_c(dag.dag_id, CYAN)}")
        # validate_asl_from_dag turns its own failures into errors; it does not raise
        is_valid, errors, warnings = validate_asl_from_dag(dag, verbose=verbose)
        if not is_valid:
            all_valid = False
            all_errors.extend(errors)
    return all_valid, all_errors


def same_account_role_errors(dag, *, account: str, default_task_role: Optional[str] = None) -> List[str]:
    """Message C for each task of ``dag`` whose ``role=`` names a role of the platform's ``account`` outside
    ``/polyris-tasks/`` (D11, ``platform.refused_task_role``). ``default_task_role`` is the name of the
    platform's default task role, when the platform is known."""
    from polyris.generators import _resolved_role

    errors = []
    for t in dag.tasks:
        role = _resolved_role(t) if getattr(t, 'role', None) else None
        if role and refused_task_role(role, account=account, default_task_role=default_task_role):
            errors.append(task_role_refusal(t.task_id, role))
    return errors


def validate_asl_from_dag(dag, verbose: bool = False) -> tuple:
    """Validate a single DAG object."""
    from polyris.generators import build_spec_version, generate_step_function_json, validate_asl
    from polyris import limits
    from polyris.config import config
    from polyris.coordination import trigger_rules
    import json as _json

    # Initialised before try so the except clause can include any errors
    # already collected when generate_step_function_json raises.
    role_errors: list = []
    trigger_rule_errors: list = []
    placeholder_errors: list = []

    try:
        # role must be 'same', a key in config.roles (pyproject.toml), or a
        # raw ARN passed through directly. The common case is a key (see the
        # role field's own inline comment) — but `polyris.roles`'
        # documented usage (`role=roles["data_warehouse"]`) passes the
        # *resolved ARN value* directly, which the task's spec (_resolved_role) already
        # accepts by falling through unresolved-as-is when a role isn't a
        # config.roles key. A typo'd role KEY isn't caught anywhere else: it
        # silently passes through generation as a plain string and only fails
        # at AWS runtime, deep in the wrapper's execution, with a much less
        # helpful error than catching it here would give — so an unrecognized
        # non-ARN-looking value is a real error, not a warning. A value that
        # looks like an ARN is accepted without a config.roles lookup, since
        # there's nothing to look up — it's already resolved.
        role_errors = []
        for t in dag.tasks:
            if (t.role and t.role != 'same' and t.role not in config.roles
                    and not t.role.startswith('arn:')):
                known = sorted(config.roles) if config.roles else '(none configured)'
                role_errors.append(
                    f"Task '{t.task_id}': role={t.role!r} is not 'same', not a "
                    f"key in config.roles, and not an ARN. Known roles: {known}"
                )
        # A role of the platform's account outside /polyris-tasks/ (D11), wherever the account is known:
        # here from config.py's account_id; polyris-deploy checks again with the caller's account.
        account = config.get('account_id')
        if account:
            role_errors.extend(same_account_role_errors(dag, account=str(account)))
        reserved = reserved_machine(dag.dag_id)
        if reserved:
            role_errors.append(
                f"DAG '{dag.dag_id}': its state machine would be named like the platform's own "
                f"'{reserved}' (<namespace>-<stage>-polyris-{reserved}), which no pipeline may start. "
                f"Pick another dag_id."
            )

        # trigger_rule is a Literal (type-checker-only), not enforced at run time: a name the engine does
        # not know would fail its task when the run reaches it (ADR #138 §2). Catch it here, with the
        # hint for a name ADR #138 turned down, from the module that evaluates the rules.
        trigger_rule_errors = []
        for t in dag.tasks:
            if t.trigger_rule and t.trigger_rule not in trigger_rules.KNOWN_RULES:
                hint = trigger_rules.REJECTED.get(t.trigger_rule)
                if hint:
                    trigger_rule_errors.append(
                        f"Task '{t.task_id}': trigger_rule={t.trigger_rule!r} is not supported (ADR #138). "
                        f"Use {hint}."
                    )
                else:
                    trigger_rule_errors.append(
                        f"Task '{t.task_id}': trigger_rule={t.trigger_rule!r} is not "
                        f"a recognized rule. Valid rules: {sorted(trigger_rules.KNOWN_RULES)}"
                    )

        # Detect placeholder ARNs generated by polyris-init. ACCOUNT_ID and
        # REGION are the sentinel tokens we embed in generated templates.
        # Only check the region (part 4) and account (part 5) segments of the
        # ARN — not the resource name — so a pipeline named "REGION-processor"
        # is not falsely blocked after the user replaces the real placeholders.
        # ARN format: arn:partition:service:region:account:resource
        for t in dag.tasks:
            arn = getattr(t, 'arn', '') or ''
            parts = arn.split(':')
            arn_region = parts[3] if len(parts) > 3 else ''
            arn_account = parts[4] if len(parts) > 4 else ''
            if arn_region == 'REGION' or arn_account == 'ACCOUNT_ID':
                unfilled = [tok for tok, present in [('REGION', arn_region == 'REGION'), ('ACCOUNT_ID', arn_account == 'ACCOUNT_ID')] if present]
                placeholder_errors.append(
                    f"Task '{t.task_id}' has a placeholder ARN — replace "
                    f"{' and '.join(unfilled)} with real values before deploying:\n"
                    f"     {arn}"
                )

        # A ${...} in a task's ARN or name: nothing substitutes it since 1.3, so the task would fail at its run.
        placeholder_errors.extend(unresolved_message(rr) for rr in unresolved_refs(dag))

        asl_json = generate_step_function_json(dag)
        asl = _json.loads(asl_json)
        is_valid, errors, warnings = validate_asl(asl)
        # The limits of ADR #137 §8, measured on the specs polyris-deploy writes.
        specs = build_spec_version(dag)
        limit_errors = limits.spec_errors(specs)
        warnings = warnings + limits.spec_warnings(specs)
        errors = role_errors + trigger_rule_errors + placeholder_errors + limit_errors + errors
        is_valid = (is_valid and not role_errors and not trigger_rule_errors and not placeholder_errors
                    and not limit_errors)

        if verbose:
            print(f"  Schedule: {_c(str(dag.schedule), CYAN) if dag.schedule else 'manual'}")
            print(f"  Tasks: {len(dag.tasks)}")
            if errors:
                print(_c(f"  ❌ Errors ({len(errors)}):", RED))
                for e in errors:
                    print(f"     • {e}")
            if warnings:
                print(_c(f"  ⚠️  Warnings ({len(warnings)}):", YELLOW))
                for w in warnings:
                    print(f"     • {w}")
            if is_valid:
                print(_c("  ✅ Valid", BOLD_GREEN))

        return is_valid, errors, warnings

    except Exception as e:
        return False, role_errors + trigger_rule_errors + placeholder_errors + [str(e)], []



def _run_test(dag_file: str) -> bool:
    """Run each task's Python body locally, and report the ones that raise.

    Returns True if all callables succeeded, False if any raised.
    """
    from contextlib import ExitStack

    from polyris.loader import PipelineLoadError, PipelineNotFoundError, open_pipeline

    with ExitStack() as stack:
        try:
            # Task bodies run while the file is open, so they can import a sibling module
            dags = stack.enter_context(open_pipeline(dag_file)).dags
        except PipelineNotFoundError:
            print(f"{_c('❌ Pipeline file not found:', RED)} {_c(dag_file, CYAN)}")
            sys.exit(1)
        except PipelineLoadError as e:
            print(f"{_c('❌ Failed to load', RED)} {_c(dag_file, CYAN)}: {e}")
            sys.exit(1)
        return _run_test_bodies(dag_file, dags)


def _run_test_bodies(dag_file: str, dags: list) -> bool:
    """Run each task's python_callable in order; True when every one succeeded."""

    if not dags:
        print(f"{_c('❌ No DAG found in', RED)} {_c(dag_file, CYAN)}")
        sys.exit(1)

    failures = []
    for dag in dags:
        print(f"{_c('Testing DAG:', BOLD)} {_c(dag.dag_id, CYAN)}")
        for task in dag.topological_sort():
            print(f"  Task: {_c(task.task_id, CYAN)}")
            if hasattr(task, 'python_callable') and task.python_callable:
                try:
                    result = task.python_callable()
                    print(f"    {_c('✅ Result:', BOLD_GREEN)} {result}")
                except Exception as e:
                    print(f"    {_c('❌ Error:', RED)} {e}")
                    failures.append((task.task_id, e))
            else:  # pragma: no cover -- every @task.* carries its decorated python_callable; this guards a task shape the DSL does not produce
                print(f"    ℹ️  No python_callable (task type: {_c(getattr(task, 'task_type', 'unknown'), CYAN)})")
    return not failures


def _print_error(e: str) -> None:
    """Print a validation error with ANSI colour when stdout is a TTY.

    Errors may be single-line (role / trigger_rule) or two-line (placeholder:
    first line is the human message, second line is the offending ARN). The
    first line is printed in bold red; the ARN line (if present) in yellow so
    it stands out as the value to fix.
    """
    lines = e.split('\n', 1)
    print(f"  ❌ {_c(lines[0], BOLD, RED)}")
    if len(lines) > 1:
        print(_c(lines[1], YELLOW))


def main():
    """
    CLI entry point for polyris-validate command.

    Usage:
        polyris-validate                    # Validate dag.py in current directory
        polyris-validate -v                 # Verbose output
        polyris-validate --all              # Validate all pipelines found
        polyris-validate --all -v           # All pipelines, verbose
        polyris-validate --json             # Output as JSON
    """
    import argparse
    import json

    parser = argparse.ArgumentParser(
        prog='polyris-validate',
        description='Validate polyris pipeline(s)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  polyris-validate              # Validate dag.py in current directory
  polyris-validate -v           # Verbose output with details
  polyris-validate --all        # Find and validate all pipelines
  polyris-validate --all -v     # All pipelines, verbose
  polyris-validate --json       # Output as JSON"""
    )
    parser.add_argument(
        '--all', '-a',
        action='store_true',
        help='Validate the pipelines of every folder in the current directory, as polyris-deploy --all deploys them'
    )
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Show detailed output (schedule and task count)'
    )
    parser.add_argument(
        '--file', '-f',
        default='dag.py',
        help='Pipeline file to validate (default: dag.py)'
    )
    parser.add_argument(
        '--test',
        action='store_true',
        help="Run each task's Python body locally and report the ones that raise"
    )
    parser.add_argument(
        '--json',
        action='store_true',
        help='Output results as JSON'
    )

    colorize_parser(parser)
    args = parser.parse_args()

    # --json requires clean stdout. Suppress verbose prints when --json is
    # active so that JSON consumers are not handed mixed text + JSON output.
    # Scripts that pass --verbose globally still work when --json is added.
    effective_verbose = args.verbose and not args.json

    if args.all:
        # Each folder's process also runs the per-file ASL/role/trigger_rule checks, on the DAGs
        # it loaded: no file is run in this process, nor twice (ADR #139)
        results = validate_all('.', verbose=effective_verbose, check_asl=True, quiet=args.json)
        if not results['pipelines'] and not results['errors']:
            # validate_all added an informative warning; surface it without
            # contaminating --json stdout.
            if args.json:
                print(json.dumps({
                    'errors': [],
                    'warnings': results['warnings'],
                    'pipeline_count': 0,
                }, indent=2))
            else:
                for w in results['warnings']:
                    print(f"⚠️  {w}")
            sys.exit(1)
        if args.json:
            print(json.dumps({
                'errors': results['errors'],
                'warnings': results['warnings'],
                'pipeline_count': len(results['pipelines']),
            }, indent=2))
        elif not effective_verbose:
            # Non-verbose non-json: print every error and warning — a pipeline file --all did
            # not load is a warning, and "no issues" over it would hide it — then a summary
            # line on success. Verbose mode surfaced all of them in validate_all's summary.
            for e in results['errors']:
                _print_error(e)
            for w in results['warnings']:
                print(_c(f"⚠️  {w}", YELLOW))
            if not results['errors']:
                found = f"{len(results['warnings'])} warning(s)" if results['warnings'] else "no issues"
                print(_c(f"✅ Validated {len(results['pipelines'])} pipeline(s) — {found}", BOLD_GREEN))
        sys.exit(0 if not results['errors'] else 1)

    elif args.test:
        result = _run_test(args.file)
        if not result:
            sys.exit(1)
        print(_c("✅ All tasks passed", BOLD_GREEN))

    else:
        valid, errors = _validate_single(args.file, verbose=effective_verbose)
        if not valid and not args.json and not effective_verbose:
            for e in errors:
                _print_error(e)
        if args.json:
            print(json.dumps({'file': args.file, 'valid': valid, 'errors': errors}, indent=2))
        elif valid and not effective_verbose:
            # Silent success was confusing — the user sees the shell prompt
            # return with no output and can't tell if the command ran or
            # scrolled past its output. Emit a positive one-liner.
            print(f"{_c('✅ Valid:', BOLD_GREEN)} {_c(args.file, CYAN)}")
        sys.exit(0 if valid else 1)


if __name__ == '__main__':  # pragma: no cover -- module entrypoint
    main()
