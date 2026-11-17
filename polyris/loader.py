"""One way to run a pipeline file and find the DAGs it defines (ADR #139).

``polyris-validate``, ``polyris-deploy``, ``polyris-output`` and ``polyris-local`` all load a
pipeline file through ``load_pipeline``, so a file they disagree about cannot exist: until
1.2.2 each had its own loader, and three times a file passed ``polyris-validate`` and then
could not be deployed (backlog F18 and the two reviews of its fix).
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import List, Union

from .dag import DAG, recording_dags

#: Modules a pipeline may import from its project, dropped after each load so the next
#: file imports its own.
_PROJECT_MODULES = ("pipelines", "pipelines.config")


class PipelineLoadError(Exception):
    """A pipeline file could not be run, or defines one dag_id twice; the message says why."""


class PipelineNotFoundError(PipelineLoadError):
    """The pipeline file does not exist."""


@dataclass
class LoadedPipeline:
    path: Path
    module: ModuleType
    #: Every DAG the file's code made, in the order it made them (``recording_dags``).
    dags: List[DAG]


def load_pipeline(path: Union[str, Path], *, module_name: str = "pipeline") -> LoadedPipeline:
    """Run the pipeline file at ``path`` and return the DAGs it defines.

    The file's folder and the folder above it are importable while it runs, so a pipeline can
    import a sibling module or its project's ``config``. ``sys.exit(0)`` (or a bare
    ``sys.exit()``) ends the file normally. Raises ``PipelineNotFoundError`` for a missing
    file and ``PipelineLoadError`` for an error, a non-zero exit, or one dag_id made twice:
    each DAG is one stack, so the second would replace the first. A file without DAGs loads
    with an empty list; the caller says what that means for it.
    """
    path = Path(path)
    if not path.is_file():
        raise PipelineNotFoundError(f"Pipeline file not found: {path}")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover -- defensive: returns a loaded spec for an existing .py file
        raise PipelineLoadError(f"Cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)

    added = [p for p in (str(path.parent), str(path.parent.parent)) if p not in sys.path]
    for p in added:
        sys.path.insert(0, p)
    try:
        with recording_dags(module) as made:
            try:
                spec.loader.exec_module(module)
            except SystemExit as e:
                if e.code not in (0, None):
                    raise PipelineLoadError(f"sys.exit({e.code})") from None
            except Exception as e:
                raise PipelineLoadError(str(e) or type(e).__name__) from e
    finally:
        for p in added:
            if p in sys.path:
                sys.path.remove(p)
        for name in _PROJECT_MODULES:
            sys.modules.pop(name, None)

    ids = [d.dag_id for d in made]
    twice = sorted({i for i in ids if ids.count(i) > 1})
    if twice:
        raise PipelineLoadError(
            f"{path.name} defines {', '.join(repr(i) for i in twice)} more than once: "
            "each DAG is one stack, so the second would replace the first"
        )
    return LoadedPipeline(path=path, module=module, dags=list(made))
