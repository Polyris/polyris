"""A folder's own Python process, for ``--all`` (ADR #139).

``polyris-deploy --all`` and ``polyris-validate --all`` load each folder's pipeline files in a
fresh Python process, so nothing one folder imports — its ``settings.py``, a path it adds, a
library's import-time side effects — reaches the next folder. ``run`` starts that process and
reads back its answer; ``main`` is the process. The request and the answer are JSON files named
on the command line: nothing is passed through the environment, so a process a pipeline starts
never mistakes itself for a folder's process.
"""

from __future__ import annotations

import json
import os
import signal
import site
import subprocess
import sys
import sysconfig
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Literal, Optional

#: Which output of the folder's process reaches the user: all of it, its stderr only, or none.
Output = Literal["all", "stderr", "none"]

#: What a folder's process does: ``validation.collect_folder`` or ``deploy.deploy_directory``.
Command = Literal["collect", "deploy"]


@dataclass
class Answer:
    """What a folder's process answered, or why it did not."""

    payload: Any
    #: Set when the process ended without answering: the folder's failure, whatever its exit code.
    error: Optional[str] = None


def run(command: Command, request: Dict[str, Any], *, output: Output = "all") -> Answer:
    """Run ``command`` (``"collect"`` or ``"deploy"``) for one folder in its own process.

    If this process is stopped (Ctrl-C, or SIGTERM under ``sigterm_stops_children``), the
    folder's process is stopped too.
    """
    fd, request_path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    fd, answer_path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        Path(request_path).write_text(json.dumps({"command": command, **request}))
        hidden = subprocess.DEVNULL
        code = _run_child(
            ["-m", "polyris.folder_process", request_path, answer_path], _child_env(),
            stdout=None if output == "all" else hidden, stderr=hidden if output == "none" else None,
        )
        try:
            return Answer(json.loads(Path(answer_path).read_text()))
        except (OSError, ValueError):  # the file mkstemp made stays empty until the process answers
            return Answer(None, f"its process exited with code {code} before reporting")
    finally:
        os.unlink(request_path)
        os.unlink(answer_path)


def main(argv: List[str]) -> int:
    """A folder's own process: do what the request file says, write the answer file."""
    request_path, answer_path = argv
    request = json.loads(Path(request_path).read_text())
    command = request.pop("command")
    from .config import use_stage

    use_stage(request.get("stage"))  # --stage names the stage of the folder's files too
    try:
        if command == "collect":
            from .validation import collect_folder

            request.pop("stage", None)
            payload: Any = collect_folder(**request)
        elif command == "deploy":
            from .deploy import deploy_directory

            payload = deploy_directory(**request)
        else:
            raise ValueError(f"unknown folder process command: {command!r}")
    except KeyboardInterrupt:  # the parent was stopped too and reports it: no second traceback
        return 130
    Path(answer_path).write_text(json.dumps(payload))
    return 0


def _child_env() -> Dict[str, str]:
    """The environment of a folder's process: this one's, so that it imports this very polyris.

    When polyris is installed (site-packages), the child finds it there as this process did.
    When it comes from elsewhere (a checkout run from source), its folder is appended to
    PYTHONPATH, after the user's own entries. Never prepended: on an install that would put
    site-packages ahead of a user's ``src/`` and of the standard library.
    """
    import polyris

    env = dict(os.environ)
    code_root = Path(polyris.__file__).resolve().parents[1]
    installed = {Path(p).resolve() for p in (*site.getsitepackages(), site.getusersitepackages(),
                                             sysconfig.get_paths()["purelib"], sysconfig.get_paths()["platlib"])}
    if code_root not in installed:
        env["PYTHONPATH"] = os.pathsep.join(p for p in (os.environ.get("PYTHONPATH", ""), str(code_root)) if p)
    return env


def _run_child(argv: List[str], env: Dict[str, str], *, stdout: Optional[int] = None,
              stderr: Optional[int] = None) -> int:
    """Run ``python -P <argv>`` to its end; its exit code.

    ``-P`` keeps the current directory off the child's ``sys.path`` while polyris starts, as it
    is off this process's when run as ``polyris-deploy``: otherwise an ``email/`` or ``json.py``
    in the run directory breaks polyris's own imports. While a file loads, its folder and the
    folder above come first, as for every command (``loader.open_pipeline``).
    """
    sys.stdout.flush()  # what this process printed comes before the child's output
    proc = subprocess.Popen([sys.executable, "-P", *argv], env=env, stdout=stdout, stderr=stderr)
    try:
        return proc.wait()
    except BaseException:
        proc.terminate()
        proc.wait()
        raise


def _raise_on_sigterm(signum: int, frame: object) -> None:
    raise SystemExit(128 + signum)


@contextmanager
def sigterm_stops_children() -> Iterator[None]:
    """Within the block, SIGTERM ends this process through Python, so ``_run_child`` stops the
    running child. For the parent only: a folder's process keeps SIGTERM's default, which ends
    it. Only the main thread can set a signal handler; elsewhere the default stays."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.signal(signal.SIGTERM, _raise_on_sigterm)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _exit_now(code: int) -> None:  # pragma: no cover -- ends the process (os._exit); runs only as a folder's process exits
    """End a folder's process once its answer is written: flush, and do not wait for non-daemon
    threads a pipeline started, which would hold the next folder."""
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


if __name__ == "__main__":  # pragma: no cover -- the folder's process itself
    _exit_now(main(sys.argv[1:]))
