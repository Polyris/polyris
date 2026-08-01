"""
ARN Resolver - Resolves task ARNs from tasks.json.
"""

from typing import Dict, List, Optional, TYPE_CHECKING
from pathlib import Path
import json

if TYPE_CHECKING:
    from .task import Task


class ARNResolver:
    """
    Resolves task ARNs from tasks.json file.
    Used by CLI to validate ARN references.
    """
    def __init__(self, pipeline_path: Optional[Path] = None):
        self.arns: Dict[str, str] = {}
        self.pipeline_path = pipeline_path
        if pipeline_path:
            self._load_tasks_json(pipeline_path)
    
    def _load_tasks_json(self, pipeline_path: Path):
        """Load ARNs from tasks.json in same directory as pipeline."""
        tasks_json = pipeline_path.parent / "tasks.json"
        if tasks_json.exists():
            try:
                with open(tasks_json) as f:
                    data = json.load(f)
                    # Support both flat and nested formats
                    if isinstance(data, dict):
                        for key, value in data.items():
                            if isinstance(value, str):
                                self.arns[key] = value
                            elif isinstance(value, dict) and 'arn' in value:
                                self.arns[key] = value['arn']
            except Exception as e:
                print(f"Warning: Could not load tasks.json: {e}")
    
    def resolve(self, arn_template: str) -> str:
        """Resolve ${task_name_arn} to actual ARN."""
        if not arn_template.startswith("${") or not arn_template.endswith("}"):
            return arn_template
        
        key = arn_template[2:-1]  # Remove ${ and }
        return self.arns.get(key, arn_template)
    
    def validate(self, tasks: List['Task']) -> List[str]:
        """Return list of unresolved ARN references."""
        unresolved = []
        for task in tasks:
            if task.arn.startswith("${") and task.arn.endswith("}"):
                key = task.arn[2:-1]
                if key not in self.arns:
                    unresolved.append(key)
        return unresolved


# Global resolver instance
_resolver: Optional[ARNResolver] = None


def set_resolver(pipeline_path: Path) -> None:
    """Set up ARN resolver for pipeline."""
    global _resolver
    _resolver = ARNResolver(pipeline_path)


def get_resolver() -> ARNResolver:
    """Get current ARN resolver."""
    global _resolver
    if _resolver is None:
        _resolver = ARNResolver()
    return _resolver
