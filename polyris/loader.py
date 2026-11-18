"""One way to run a pipeline file and find the DAGs it defines, and which files ``--all`` loads
(ADR #139).

``polyris-validate``, ``polyris-deploy``, ``polyris-output`` and ``polyris-local`` all load a
pipeline file through ``load_pipeline``, so a file they disagree about cannot exist: until
1.2.2 each had its own loader, and a file could pass ``polyris-validate`` and then fail to
deploy. ``polyris-validate --all`` and ``polyris-deploy --all`` find files the same way
(``pipeline_folders``, ``files_in``, ``is_pipeline_file``).
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, Iterable, Iterator, List, Set, Tuple, Union

from .dag import DAG

#: The ``__name__`` every pipeline file runs under, whichever command loads it.
MODULE_NAME = "polyris_pipeline"

#: pytest's conftest and package markers: never loaded as pipelines.
_NOT_PIPELINES = ("conftest.py", "__init__.py")

#: What a pipeline file imports from polyris to make a DAG (``polyris/__init__.py`` exports them).
_DAG_MAKERS = frozenset({"DAG", "dag", "Pipeline"})

#: The modules a ``from … import *`` gets a DAG maker from: ``polyris``, ``polyris.dag`` (``DAG``,
#: ``Pipeline``) and ``polyris.helpers`` (``dag``). A star import of ``polyris.xcom`` makes no DAG.
_DAG_MAKER_MODULES = frozenset({"polyris", "polyris.dag", "polyris.helpers"})

#: In the text of a file ``ast`` cannot parse: ``from polyris[.x] import …`` (one line, or a
#: parenthesised list over several), ``polyris[.x].DAG``, a comment, and a DAG maker's name.
_FROM_POLYRIS_IMPORT = re.compile(
    rb"^[ \t]*from[ \t]+(polyris(?:\.\w+)*)[ \t]+import[ \t]*(\([^)]*\)|[^\n]*)", re.MULTILINE)
_POLYRIS_ATTRIBUTE = re.compile(rb"\bpolyris(?:\.\w+)*\.(?:DAG|dag|Pipeline)\b")
_COMMENT = re.compile(rb"#[^\n]*")
_DAG_MAKER_NAME = re.compile(rb"\b(?:DAG|dag|Pipeline)\b")

#: Folders ``--all`` never looks in: caches, installed code, and hidden ones (``.venv``, ``.git``).
_NOT_PROJECT_FOLDERS = frozenset({"__pycache__", "node_modules", "site-packages"})

#: The frame of an ``import`` statement (and ``importlib.import_module``) in progress.
_IMPORTING = ("importlib._bootstrap", "_find_and_load")


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
    """Whether ``--all`` loads this .py file: ``dag.py``, or a file whose code imports ``DAG``,
    ``dag`` or ``Pipeline`` from polyris.

    The file is parsed from its bytes, as Python reads it, and never run: a Glue, Lambda or ECS
    script that imports only ``xcom`` is not a pipeline whatever its comments say, and a
    pipeline always imports what makes its DAG. A pipeline that imports its DAG from a project
    wrapper is skipped; name it ``dag.py`` or deploy it with ``--file``. A file that does not
    parse is loaded when its text (comments aside) imports a DAG maker from polyris, so a
    broken pipeline's error is reported, while a script written for a newer Python that imports
    only ``xcom`` is not run. Test files by pytest's convention (``test_*.py``, ``*_test.py``,
    ``conftest.py``) and ``__init__.py`` are not pipelines.
    """
    name = path.name
    if name in _NOT_PIPELINES or name.startswith("test_") or name.endswith("_test.py"):
        return False
    if name == "dag.py":
        return True
    try:
        source = path.read_bytes()  # bytes: ast honours a BOM and a coding line, as Python does
    except OSError:  # unreadable (permissions): not loaded, and listed as skipped by the caller
        return False
    try:
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, ValueError, RecursionError, MemoryError):  # ValueError: a null byte
        return _text_imports_a_dag_maker(source)
    return _imports_a_dag_maker(tree)


def _text_imports_a_dag_maker(source: bytes) -> bool:
    """``_imports_a_dag_maker`` for a file that does not parse, read from its text."""
    text = _COMMENT.sub(b"", source)
    for match in _FROM_POLYRIS_IMPORT.finditer(text):
        module, names = match.group(1).decode(), match.group(2)
        if _DAG_MAKER_NAME.search(names) or (b"*" in names and module in _DAG_MAKER_MODULES):
            return True
    return _POLYRIS_ATTRIBUTE.search(text) is not None


def _imports_a_dag_maker(tree: ast.AST) -> bool:
    """``from polyris[.x] import DAG|dag|Pipeline`` (``*`` from a module that exports one), or ``import polyris[ as p]`` and
    ``polyris.DAG`` (``p.dag``, …) used in the code."""
    module_names: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if (node.module == "polyris" or node.module.startswith("polyris.")) and any(
                    a.name in _DAG_MAKERS or (a.name == "*" and node.module in _DAG_MAKER_MODULES)
                    for a in node.names):
                return True
        elif isinstance(node, ast.Import):
            module_names.update(a.asname or a.name.split(".")[0] for a in node.names
                                if a.name == "polyris" or a.name.startswith("polyris."))
    return bool(module_names) and any(
        isinstance(node, ast.Attribute) and node.attr in _DAG_MAKERS
        and isinstance(node.value, ast.Name) and node.value.id in module_names
        for node in ast.walk(tree)
    )


def _is_project_folder(name: str) -> bool:
    return not name.startswith(".") and name not in _NOT_PROJECT_FOLDERS


def pipeline_folders(run_dir: Union[str, Path]) -> List[Path]:
    """The folders ``--all`` loads (``polyris-validate`` and ``polyris-deploy``): each folder
    directly in ``run_dir``, but hidden ones (``.venv``, ``.git``) and caches."""
    root = Path(run_dir)
    if not root.is_dir():
        return []
    return [p for p in sorted(root.iterdir()) if p.is_dir() and _is_project_folder(p.name)]


@dataclass
class FolderFiles:
    #: The folder's pipeline files (``is_pipeline_file``), in name order.
    pipelines: List[Path]
    #: Its other .py files, which ``--all`` lists and never runs.
    others: List[Path]


def files_in(folder: Union[str, Path]) -> FolderFiles:
    """The .py files directly in ``folder``, split into pipeline files and the others."""
    candidates = sorted(Path(folder).glob("*.py"))
    pipelines = [p for p in candidates if is_pipeline_file(p)]
    return FolderFiles(pipelines, [p for p in candidates if p not in pipelines])


def pipelines_out_of_reach(run_dir: Union[str, Path]) -> List[Path]:
    """Pipeline files ``--all`` does not load: in ``run_dir`` itself, or deeper than one folder
    below it. Both commands warn about them, so a pipeline that validates also deploys."""
    root = Path(run_dir)
    found: List[Path] = []
    for folder, subfolders, names in os.walk(root):
        subfolders[:] = sorted(d for d in subfolders if _is_project_folder(d))
        if len(Path(folder).relative_to(root).parts) == 1:
            continue  # a folder --all loads
        found.extend(p for p in (Path(folder) / n for n in sorted(names) if n.endswith(".py"))
                     if is_pipeline_file(p))
    return found


def duplicate_dag_ids(made: Iterable[Tuple[str, Any]]) -> List[str]:
    """One message per dag_id that more than one file makes, from ``(dag_id, file)`` pairs.

    Each DAG is one stack, so one would replace the other. Within one file, ``open_pipeline``
    already refuses it; across files and folders, ``--all`` does."""
    files: Dict[str, List[str]] = {}
    for dag_id, file in made:
        files.setdefault(dag_id, []).append(str(file))
    return [f"dag_id {dag_id!r} is made by {' and '.join(names)}: each DAG is one stack, "
            "so one would replace the other"
            for dag_id, names in sorted(files.items()) if len(names) > 1]


def _made_in_an_import(frame: Any, stop: Any) -> bool:
    """Whether an ``import`` ran between ``frame`` and ``stop`` (the loader's own frame).

    A DAG a module makes while Python imports it is that module's: a module the pipeline file
    imports, or a copy of the file a helper imports back. Anything else the file's code runs
    is the file's — a call, a factory in another module, ``exec``, ``runpy``, a file it loads
    with ``spec.loader.exec_module`` — and so is a worker thread it started, whose stack never
    reaches ``stop``.
    """
    while frame is not None and frame is not stop:
        if (frame.f_globals.get("__name__"), frame.f_code.co_name) == _IMPORTING:
            return True
        frame = frame.f_back
    return False


@contextmanager
def recording_dags(stop: Any) -> Iterator[List[DAG]]:
    """Record every DAG made while this block runs, outside an ``import``, in order.

    ``stop`` is ``open_pipeline``'s own frame. A DAG is recorded when it is made
    (``DAG.__post_init__``), so it counts with or without ``with``, bound to a name or not,
    unless an ``import`` made it (``_made_in_an_import``). A DAG whose ``with`` block raised an
    error does not count; ``sys.exit(0)`` in the block or a closed generator does not drop it.
    """
    made: List[DAG] = []
    raised: Set[int] = set()
    original_post_init = DAG.__post_init__
    original_exit = DAG.__exit__

    def recording_post_init(self: DAG) -> None:
        original_post_init(self)
        if not _made_in_an_import(sys._getframe(1), stop):
            made.append(self)

    def recording_exit(self: DAG, exc_type: Any, exc_val: Any, exc_tb: Any) -> Any:
        # An error drops the DAG; sys.exit(0) or a generator closed inside the block do not
        if exc_type is not None and issubclass(exc_type, Exception):
            raised.add(id(self))
        return original_exit(self, exc_type, exc_val, exc_tb)

    DAG.__post_init__ = recording_post_init  # type: ignore[method-assign]  # deliberate: instrument for discovery, restored in finally
    DAG.__exit__ = recording_exit  # type: ignore[method-assign]  # deliberate: instrument for discovery, restored in finally
    try:
        yield made
    finally:
        DAG.__post_init__ = original_post_init  # type: ignore[method-assign]  # deliberate: restore the method
        DAG.__exit__ = original_exit  # type: ignore[method-assign]  # deliberate: restore the method
        made[:] = [d for d in made if id(d) not in raised]


@contextmanager
def open_pipeline(path: Union[str, Path]) -> Iterator[LoadedPipeline]:
    """Run the pipeline file at ``path``; inside the block, its imports stay as the file left them.

    The file's folder and then the folder above it come first on ``sys.path`` while it runs and
    while the block runs, so a pipeline imports a sibling module before its project's, and a task body
    that ``polyris-local`` or ``polyris-validate --test`` runs can too; on leaving the block they
    are removed. Modules the file imports stay imported: ``--all`` runs each folder in its own
    process (``polyris.folder_process``), so nothing one folder imports reaches the next.
    ``sys.exit(0)`` (or a bare ``sys.exit()``) ends the file normally. Raises ``PipelineNotFoundError`` for a missing file and
    ``PipelineLoadError`` for an error, a non-zero exit, or one dag_id made twice: each DAG is
    one stack, so the second would replace the first. A file without DAGs loads with an empty
    list; the caller says what that means for it.
    """
    given = path
    path = Path(os.path.abspath(path))  # absolute and without "..": "dag.py" has a folder above it
    if not path.is_file():
        raise PipelineNotFoundError(f"Pipeline file not found: {given}")
    spec = importlib.util.spec_from_file_location(MODULE_NAME, path)
    if spec is None or spec.loader is None:  # pragma: no cover -- defensive: returns a loaded spec for an existing .py file
        raise PipelineLoadError(f"Cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)

    # The file's own folder first, as Python puts a script's folder first, then the folder above:
    # a sibling config.py wins over the project root's
    added = [p for p in (str(path.parent), str(path.parent.parent)) if p not in sys.path]
    sys.path[0:0] = added
    try:
        with recording_dags(sys._getframe()) as made:
            try:
                spec.loader.exec_module(module)
            except SystemExit as e:
                if e.code not in (0, None):
                    raise PipelineLoadError(f"sys.exit({e.code})") from None
            except Exception as e:
                raise PipelineLoadError(str(e) or type(e).__name__) from e
        ids = [d.dag_id for d in made]
        twice = sorted({i for i in ids if ids.count(i) > 1})
        if twice:
            raise PipelineLoadError(
                f"{path.name} defines {', '.join(repr(i) for i in twice)} more than once: "
                "each DAG is one stack, so the second would replace the first"
            )
        yield LoadedPipeline(path=path, module=module, dags=list(made))
    finally:
        for p in added:
            if p in sys.path:
                sys.path.remove(p)


def load_pipeline(path: Union[str, Path]) -> LoadedPipeline:
    """``open_pipeline`` for a caller that only needs the DAGs: cleaned up on return."""
    with open_pipeline(path) as loaded:
        return loaded
