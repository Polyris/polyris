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

import sys
import importlib.util
from pathlib import Path
from typing import Dict, List, Set, Tuple, Optional, Any
from dataclasses import dataclass, field


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


def discover_pipeline_files(directory: str) -> List[str]:
    """
    Find all Python files that might contain pipeline definitions.
    
    Looks for:
    - dag.py files in subdirectories
    - *_pipeline.py, *_dag.py files
    - Files containing 'DAG(' pattern
    
    Args:
        directory: Root directory to search
        
    Returns:
        List of file paths
    """
    pipeline_files = []
    root = Path(directory)
    
    if not root.exists():
        return []
    
    for path in root.rglob('*.py'):
        # Skip test files, __pycache__, etc
        if '__pycache__' in str(path) or 'test' in path.name.lower():
            continue
            
        # Include dag.py files (default pipeline file)
        if path.name == 'dag.py':
            pipeline_files.append(str(path))
            continue
            
        # Include files with pipeline/dag in name
        if 'pipeline' in path.name.lower() or 'dag' in path.name.lower():
            pipeline_files.append(str(path))
            continue
            
        # Check file content for DAG definition
        try:
            content = path.read_text()
            if 'with DAG(' in content or 'DAG(' in content:
                pipeline_files.append(str(path))
        except (OSError, UnicodeDecodeError):
            # File unreadable (permissions/binary) — skip it, not a pipeline
            continue
    
    return sorted(set(pipeline_files))


def extract_dag_info(file_path: str) -> List[DAGInfo]:
    """
    Extract DAG information from a pipeline file without full import.
    
    Uses importlib to load the module and extract DAG objects.
    Imports pipeline file for DAG extraction.
    
    Args:
        file_path: Path to Python file
        
    Returns:
        List of DAGInfo objects found in file
    """
    dags = []
    
    try:
        import sys
        
        try:
            # Track DAGs created via context manager
            from polyris.dag import DAG
            created_dags = []
            original_exit = DAG.__exit__
            
            def tracking_exit(self, *args):
                created_dags.append(self)
                return original_exit(self, *args)
            
            DAG.__exit__ = tracking_exit  # type: ignore[method-assign]  # deliberate: instrument for discovery, restored in finally
            
            try:
                # Load module
                spec = importlib.util.spec_from_file_location("pipeline_module", file_path)
                if not spec or not spec.loader:  # pragma: no cover -- spec_from_file_location returns None only for a path Python cannot load; defensive
                    return []
                    
                module = importlib.util.module_from_spec(spec)
                
                # Temporarily add parent dirs to path for imports
                parent_dir = str(Path(file_path).parent)
                grandparent_dir = str(Path(file_path).parent.parent)
                added_paths = []
                for p in [parent_dir, grandparent_dir]:
                    if p not in sys.path:
                        sys.path.insert(0, p)
                        added_paths.append(p)
                
                try:
                    spec.loader.exec_module(module)
                except Exception as e:
                    err_str = str(e)
                    print(f"  Warning: {Path(file_path).name} import error: {err_str}")
                
                # Cleanup paths
                for p in added_paths:
                    if p in sys.path:
                        sys.path.remove(p)
                
                # Process created DAGs
                for dag in created_dags:
                    info = DAGInfo(
                        dag_id=dag.dag_id,
                        file_path=file_path,
                        schedule=dag.schedule,
                        is_asset_triggered=dag.is_asset_triggered
                    )
                    
                    # Extract asset trigger info
                    if dag.is_asset_triggered:
                        schedule_info = dag.asset_schedule_info
                        info.trigger_assets = schedule_info.get('assets', [])
                        info.trigger_operator = schedule_info.get('operator', 'OR')
                    
                    # Extract produced/consumed assets from tasks
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
                    
            finally:
                # Restore DAG.__exit__
                DAG.__exit__ = original_exit  # type: ignore[method-assign]  # deliberate: restore instrumented method
                
        finally:
            # Cleanup pipeline-specific mocks
            for mod in ['pipelines', 'pipelines.config']:  # pragma: no cover -- cleanup of optionally-cached pipeline modules; only runs when the pipeline imported them
                if mod in sys.modules:
                    del sys.modules[mod]
                
    except Exception as e:  # pragma: no cover -- defensive outer guard; the inner import-error handler covers the load-failure path
        print(f"Warning: Could not load {file_path}: {e}")
    
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


def validate_all(directory: str = './pipelines', verbose: bool = True) -> Dict:
    """
    Validate all pipelines in directory.
    
    Checks:
    - Asset cycles between DAGs
    - Missing asset producers (optional warning)
    
    Args:
        directory: Root directory containing pipeline files
        verbose: Print progress
        
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
        print(f"Scanning {directory}...")
    
    # Discover pipeline files
    files = discover_pipeline_files(directory)
    
    if not files:
        results['warnings'].append(f"No pipeline files found in {directory}")
        return results
    
    if verbose:
        print(f"Found {len(files)} pipeline file(s)")
    
    # Extract DAG info from each file
    all_dags = []
    for file_path in files:
        dags = extract_dag_info(file_path)
        all_dags.extend(dags)
        results['pipelines'].extend(dags)
    
    if verbose:
        print(f"Loaded {len(all_dags)} DAG(s):")
        for dag in all_dags:
            trigger = dag.schedule if not dag.is_asset_triggered else f"asset:{dag.trigger_operator}"
            print(f"  • {dag.dag_id} ({trigger})")
    
    if not all_dags:
        results['warnings'].append("No DAGs found in pipeline files")
        return results
    
    # Build asset graph
    if verbose:
        print("\nBuilding asset graph...")
    
    graph = build_asset_graph(all_dags)
    results['graph'] = graph
    
    # Detect cycles
    if verbose:
        print("Checking for asset cycles...")
    
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
            print(f"❌ {len(results['errors'])} error(s) found:")
            for err in results['errors']:
                print(f"   {err}")
        else:
            print("✓ No cycles detected")
        
        if results['warnings']:
            print(f"⚠️  {len(results['warnings'])} warning(s):")
            for warn in results['warnings']:
                print(f"   {warn}")
    
    return results


def _validate_single(dag_file: str, verbose: bool) -> bool:
    """Validate a single pipeline file. Returns True if valid."""
    import importlib.util
    from pathlib import Path

    path = Path(dag_file)
    if not path.exists():
        print(f"❌ Pipeline file not found: {dag_file}")
        return False

    try:
        spec = importlib.util.spec_from_file_location("_dag", path)
        if spec is None or spec.loader is None:  # pragma: no cover -- defensive: returns a loaded spec for existing .py paths
            raise ImportError(f"Cannot load module from {path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # Find DAGs in module
        from polyris.dag import DAG as PolyrisDAG
        dags = [v for v in vars(mod).values() if isinstance(v, PolyrisDAG)]

        if not dags:
            print(f"❌ No DAG found in {dag_file}")
            return False

        all_valid = True
        for dag in dags:
            print(f"Validating: {dag.dag_id}")
            is_valid, errors, warnings = validate_asl_from_dag(dag, verbose=verbose)
            if not is_valid:
                all_valid = False  # pragma: no cover -- generated ASL for a DSL DAG validates clean, so the invalid branch is unreachable from a valid-importing pipeline
        return all_valid

    except Exception as e:
        print(f"❌ Failed to load {dag_file}: {e}")
        return False


def validate_asl_from_dag(dag, verbose: bool = False) -> tuple:
    """Validate a single DAG object."""
    from polyris.generators import generate_step_function_json, validate_asl
    from polyris.config import config
    from polyris.constants import TriggerRuleLiteral
    from typing import get_args
    import json as _json

    try:
        # role must be 'same', a key in config.roles (pyproject.toml), or a
        # raw ARN passed through directly. The common case is a key (see the
        # role field's own inline comment listing example keys: 'acq', 'etl',
        # 'processing', 'orchestration', 'same') — but `polyris.roles`'
        # documented usage (`role=roles["data_warehouse"]`) passes the
        # *resolved ARN value* directly, which _build_task_branch already
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

        # trigger_rule is a Literal (type-checker-only) not runtime-enforced —
        # an unrecognized string silently passes through construction and only
        # surfaces at evaluate_deps' "Unknown trigger_rule; defaulting to
        # all_success" fallback (ADR #117). Catch it here with a specific
        # suggestion instead of a silent behavior change.
        valid_rules = set(get_args(TriggerRuleLiteral))
        removed_rule_suggestions = {
            'one_done': "'all_done' (identical in every reachable state)",
            'none_failed': "'all_done' (identical in every reachable state)",
            'none_failed_min_one_success': "'one_success' (identical in every reachable state)",
            'all_done_min_one_success': "'one_success' (identical in every reachable state)",
            'all_failed': "no replacement — never satisfiable (a confirmed failure cancels the whole pipeline before this rule can evaluate)",
            'one_failed': "no replacement — never satisfiable (a confirmed failure cancels the whole pipeline before this rule can evaluate)",
        }
        trigger_rule_errors = []
        for t in dag.tasks:
            if t.trigger_rule and t.trigger_rule not in valid_rules:
                suggestion = removed_rule_suggestions.get(t.trigger_rule)
                if suggestion:
                    trigger_rule_errors.append(
                        f"Task '{t.task_id}': trigger_rule={t.trigger_rule!r} was "
                        f"removed (ADR #117). Use {suggestion}."
                    )
                else:
                    trigger_rule_errors.append(
                        f"Task '{t.task_id}': trigger_rule={t.trigger_rule!r} is not "
                        f"a recognized rule. Valid rules: {sorted(valid_rules)}"
                    )

        asl_json = generate_step_function_json(dag)
        asl = _json.loads(asl_json)
        is_valid, errors, warnings = validate_asl(asl)
        errors = role_errors + trigger_rule_errors + errors
        is_valid = is_valid and not role_errors and not trigger_rule_errors

        if verbose:
            print(f"  Schedule: {dag.schedule or 'manual'}")
            print(f"  Tasks: {len(dag.tasks)}")
            print(f"  States: {len(asl.get('States', {}))}")

        if errors:  # pragma: no cover -- rare in practice (e.g. an empty DAG with
                    # zero tasks/steps produces a real 'Parallel has no branches'
                    # error, or a task references an undefined role — both
                    # verified, both reachable, not hand-written-ASL-only);
                    # deploy_pipeline's gate gets to it before AWS does either way.
            print(f"  ❌ Errors ({len(errors)}):")
            for e in errors:
                print(f"     • {e}")
        if warnings:  # pragma: no cover -- generated ASL for a DSL DAG has no warnings (the Catch-reachability fix removed the only ones); this prints validate_asl warnings that only hand-written ASL triggers
            print(f"  ⚠️  Warnings ({len(warnings)}):")
            for w in warnings:
                print(f"     • {w}")
        if is_valid:
            print("  ✅ Valid")

        return is_valid, errors, warnings

    except Exception as e:
        print(f"  ❌ Generation failed: {e}")
        return False, [str(e)], []


def _find_all_pipelines() -> list:
    """Discover all dag.py files from cwd upwards/downwards."""
    from pathlib import Path

    cwd = Path.cwd()
    dag_files = []

    # Search in common locations
    for pattern in ['**/dag.py', '**/pipeline.py']:
        for p in cwd.rglob(pattern.split('/')[-1]):
            if '__pycache__' not in str(p) and '.git' not in str(p):
                dag_files.append(str(p))

    return sorted(set(dag_files))


def _run_test(dag_file: str) -> None:
    """Run python_callable for each task (for @task.python pipelines)."""
    import importlib.util
    from pathlib import Path

    path = Path(dag_file)
    if not path.exists():
        print(f"❌ Pipeline file not found: {dag_file}")
        sys.exit(1)

    spec = importlib.util.spec_from_file_location("_dag", path)
    if spec is None or spec.loader is None:  # pragma: no cover -- defensive: returns a loaded spec for existing .py paths
        raise ImportError(f"Cannot load module from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    from polyris.dag import DAG as PolyrisDAG
    dags = [v for v in vars(mod).values() if isinstance(v, PolyrisDAG)]

    if not dags:
        print(f"❌ No DAG found in {dag_file}")
        sys.exit(1)

    for dag in dags:
        print(f"Testing DAG: {dag.dag_id}")
        for task in dag.topological_sort():
            print(f"  Task: {task.task_id}")
            if hasattr(task, 'python_callable') and task.python_callable:
                try:
                    result = task.python_callable()
                    print(f"    ✅ Result: {result}")
                except Exception as e:
                    print(f"    ❌ Error: {e}")
            else:  # pragma: no cover -- every @task.* carries its decorated python_callable; this guards a task shape the DSL does not produce
                print(f"    ℹ️  No python_callable (task type: {getattr(task, 'task_type', 'unknown')})")



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
        help='Validate all pipelines found in project'
    )
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Show detailed output (schedule, task count, state count)'
    )
    parser.add_argument(
        '--file', '-f',
        default='dag.py',
        help='Pipeline file to validate (default: dag.py)'
    )
    parser.add_argument(
        '--test',
        action='store_true',
        help='Run python_callable for each task (for @task.python pipelines)'
    )
    parser.add_argument(
        '--json',
        action='store_true',
        help='Output results as JSON'
    )

    args = parser.parse_args()

    if args.all:
        dag_files = _find_all_pipelines()
        if not dag_files:
            print("❌ No pipeline files found")
            sys.exit(1)

        print(f"Found {len(dag_files)} pipeline(s)")
        all_valid = True
        results = []
        for f in dag_files:
            valid = _validate_single(f, verbose=args.verbose)
            if not valid:
                all_valid = False
            results.append({'file': f, 'valid': valid})

        if args.json:
            print(json.dumps(results, indent=2))

        sys.exit(0 if all_valid else 1)

    elif args.test:
        _run_test(args.file)

    else:
        # Single pipeline in cwd
        valid = _validate_single(args.file, verbose=args.verbose)
        sys.exit(0 if valid else 1)


if __name__ == '__main__':  # pragma: no cover -- module entrypoint
    main()
