"""
polyris-output — Generate pipeline artifacts.

Reads dag.py from current directory and outputs:
  --json      Step Functions ASL JSON
  --mermaid   Mermaid diagram
  --graph     ASCII DAG graph
  --assets    Asset registry JSON (for asset-triggered pipelines)
"""

import sys
import importlib.util
from pathlib import Path


def _load_dags(dag_file: str):
    """Load DAG(s) from file."""
    path = Path(dag_file)
    if not path.exists():
        print(f"❌ Pipeline file not found: {dag_file}")
        sys.exit(1)

    try:
        spec = importlib.util.spec_from_file_location("_dag", path)
        if spec is None or spec.loader is None:  # pragma: no cover -- defensive: spec_from_file_location returns a loaded spec for existing .py paths
            raise ImportError(f"Cannot load module from {path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        from polyris.dag import DAG as PolyrisDAG
        dags = [v for v in vars(mod).values() if isinstance(v, PolyrisDAG)]

        if not dags:
            print(f"❌ No DAG found in {dag_file}")
            sys.exit(1)

        return dags, mod

    except Exception as e:
        print(f"❌ Failed to load {dag_file}: {e}")
        sys.exit(1)


def _select_dag(dags, select_id=None):
    """Select a single DAG from a list."""
    if select_id:
        matching = [d for d in dags if d.dag_id == select_id]
        if not matching:
            print(f"❌ DAG '{select_id}' not found")
            print(f"   Available: {[d.dag_id for d in dags]}")
            sys.exit(1)
        return matching[0]
    if len(dags) > 1:
        print(f"Multiple DAGs found: {[d.dag_id for d in dags]}")
        print("Use --select <dag_id> to choose one")
        sys.exit(1)
    return dags[0]


def main():
    """
    CLI entry point for polyris-output command.

    Usage:
        polyris-output --json       # Generate ASL JSON
        polyris-output --mermaid    # Generate Mermaid diagram
        polyris-output --graph      # Show ASCII DAG graph
        polyris-output --assets     # Generate asset registry JSON
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="polyris-output",
        description="Generate pipeline artifacts from dag.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  polyris-output --json           # Generate ASL JSON for deployment
  polyris-output --mermaid        # Generate Mermaid diagram
  polyris-output --graph          # Show DAG as ASCII graph
  polyris-output --assets         # Generate asset registry JSON
  polyris-output --json --file my_pipeline.py   # Custom file
  polyris-output --json --select my-dag         # Multi-DAG file"""
    )

    output_group = parser.add_mutually_exclusive_group(required=True)
    output_group.add_argument("--json", action="store_true",
                              help="Generate Step Functions ASL JSON")
    output_group.add_argument("--mermaid", action="store_true",
                              help="Generate Mermaid diagram")
    output_group.add_argument("--graph", action="store_true",
                              help="Show DAG as ASCII graph")
    output_group.add_argument("--assets", action="store_true",
                              help="Generate asset registry JSON (for asset-triggered pipelines)")

    parser.add_argument("--file", "-f", default="dag.py",
                        help="Pipeline file (default: dag.py)")
    parser.add_argument("--select", help="Select specific DAG by ID (for multi-DAG files)")

    args = parser.parse_args()

    dags, mod = _load_dags(args.file)

    if args.assets:
        # Assets works on all DAGs in the file
        from polyris.generators import generate_all_assets
        import json
        print(json.dumps(generate_all_assets(dags), indent=2))
        return

    dag = _select_dag(dags, args.select)

    if args.json:
        from polyris.generators import generate_step_function_json
        print(generate_step_function_json(dag))

    elif args.mermaid:
        from polyris.generators import generate_mermaid
        print(generate_mermaid(dag))

    elif args.graph:
        from polyris.generators import render_dag_ascii
        print(render_dag_ascii(dag))


if __name__ == "__main__":  # pragma: no cover -- entry-point guard; main() is exercised directly
    main()
