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
from typing import List, Optional, Set, Union

from .dag import DAG, recording_dags

#: The ``__name__`` every pipeline file runs under, whichever command loads it.
MODULE_NAME = "polyris_pipeline"


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


def is_pipeline_file(path: Path) -> bool:
    """Whether a .py file looks like a pipeline: ``dag.py``, a name with "dag" or "pipeline", or
    a file that makes a DAG (``DAG(`` or ``@dag``). Test files are not. ``polyris-validate --all``
    and ``polyris-deploy --all`` / ``--only`` load only these, so a Glue script or a helper next
    to ``dag.py`` is never run, and the two commands look at the same files (ADR #139)."""
    name = path.name.lower()
    if "test" in name:
        return False
    if name == "dag.py" or "dag" in name or "pipeline" in name:
        return True
    try:
        content = path.read_text()
    except (OSError, UnicodeDecodeError):  # unreadable (permissions, binary): not a pipeline
        return False
    return "DAG(" in content or "@dag" in content


def _folder_of(module: ModuleType) -> Optional[Path]:
    """The folder a top-level module or package was imported from."""
    paths = list(getattr(module, "__path__", []) or [])
    if paths:  # a package, namespace packages included: the folder holding its directory
        return Path(paths[0]).resolve().parent
    file = getattr(module, "__file__", None)
    return Path(file).resolve().parent if file else None


def _forget_imports(before: Set[str], folders: List[Path]) -> None:
    """Forget the modules the file imported from its own folders, so the next file imports its own.

    Only a top-level module first imported by this load, and found directly in the file's
    folder or the one above (``settings``, ``pipelines``), with its submodules. Never one
    deeper in those folders (a ``.venv`` inside the project) and never ``polyris``: a second
    copy of such a module would make a second copy of its classes.
    """
    tops = {name for name in sys.modules if "." not in name and name not in before and name != "polyris"}
    local = {top for top in tops if _folder_of(sys.modules[top]) in folders}
    for name in [n for n in sys.modules if n.split(".", 1)[0] in local]:
        del sys.modules[name]


def load_pipeline(path: Union[str, Path]) -> LoadedPipeline:
    """Run the pipeline file at ``path`` and return the DAGs it defines.

    The file's folder and the folder above it are importable while it runs, so a pipeline can
    import a sibling module or its project's ``config``; every module it imports from those
    folders is forgotten after, so the next file, in another folder, imports its own
    ``settings`` and not this one's. ``sys.exit(0)`` (or a bare ``sys.exit()``) ends the file
    normally. Raises ``PipelineNotFoundError`` for a missing file and ``PipelineLoadError`` for
    an error, a non-zero exit, or one dag_id made twice: each DAG is one stack, so the second
    would replace the first. A file without DAGs loads with an empty list; the caller says
    what that means for it.
    """
    given = path
    path = Path(path).absolute()  # a relative "dag.py" has a parent of "." and no folder above it
    if not path.is_file():
        raise PipelineNotFoundError(f"Pipeline file not found: {given}")
    spec = importlib.util.spec_from_file_location(MODULE_NAME, path)
    if spec is None or spec.loader is None:  # pragma: no cover -- defensive: returns a loaded spec for an existing .py file
        raise PipelineLoadError(f"Cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)

    folders = [path.parent.resolve(), path.parent.parent.resolve()]
    added = [p for p in (str(path.parent), str(path.parent.parent)) if p not in sys.path]
    for p in added:
        sys.path.insert(0, p)
    before = set(sys.modules)
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
        _forget_imports(before, folders)

    ids = [d.dag_id for d in made]
    twice = sorted({i for i in ids if ids.count(i) > 1})
    if twice:
        raise PipelineLoadError(
            f"{path.name} defines {', '.join(repr(i) for i in twice)} more than once: "
            "each DAG is one stack, so the second would replace the first"
        )
    return LoadedPipeline(path=path, module=module, dags=list(made))
