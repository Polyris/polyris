"""Every console read of a pipeline-tokens index names only what that index projects.

DynamoDB rejects a query whose ProjectionExpression names an attribute the index
does not project ("Global secondary index … does not project [...]"); moto returns
the rows without it. So no unit test saw that /api/tasks asked date-pipeline-index
and pipeline-date-index for fields they do not carry: every call failed with a 500
until the 1.2.0 live smoke found it. This pins the contract against
sam/template.yaml (Principle #28). Checked: the `projection=` argument of every
executions_repo index method and of feed.pipeline_rows_before, and the
ProjectionExpression of every query dict handed to executions_repo.query_by_date_raw.
A reserved word (`status`, `date`, `error`) in a projection must go through a
placeholder too: DynamoDB rejects it as well.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

from cfnlint.decode import decode
from moto.dynamodb.parsing.reserved_keywords import ReservedKeywords

REPO_ROOT = Path(__file__).resolve().parents[2]
CONSOLE_API = REPO_ROOT / "sam" / "lambdas" / "console_api"
TABLE_KEY = "execution_name"
_INDEX_NAME_RE = re.compile(r"IndexName['\"]?\s*[:=,]\s*['\"]([\w-]+)['\"]")


def _index_attributes() -> Dict[str, Optional[Set[str]]]:
    """Index name -> the attributes a query on it may name; None when it projects ALL."""
    template, matches = decode(str(REPO_ROOT / "sam" / "template.yaml"))
    assert template is not None, matches
    tables = [res["Properties"] for res in template["Resources"].values()
              if res.get("Type") == "AWS::DynamoDB::Table"
              and "pipeline-tokens" in str(res["Properties"].get("TableName"))]
    assert len(tables) == 1, "expected one pipeline-tokens table"
    indexes: Dict[str, Optional[Set[str]]] = {}
    for index in tables[0]["GlobalSecondaryIndexes"]:
        projection = index["Projection"]
        if projection["ProjectionType"] == "ALL":
            indexes[index["IndexName"]] = None
        else:
            indexes[index["IndexName"]] = ({TABLE_KEY}
                                           | {k["AttributeName"] for k in index["KeySchema"]}
                                           | set(projection.get("NonKeyAttributes", [])))
    return indexes


def _functions(path: Path) -> Iterator[Tuple[ast.FunctionDef, str]]:
    source = path.read_text()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef):
            yield node, ast.get_source_segment(source, node) or ""


def _reader_indexes() -> Dict[str, str]:
    """Reader name -> the index it queries: executions_repo methods, read from the DAL,
    and feed.pipeline_rows_before, through the DAL method it calls."""
    readers = {fn.name: m.group(1) for fn, src in _functions(CONSOLE_API / "dal" / "executions_repo.py")
               if (m := _INDEX_NAME_RE.search(src))}
    for fn, src in _functions(CONSOLE_API / "feed.py"):
        if fn.name == "pipeline_rows_before":
            called = re.search(r"executions_repo\.(\w+)\(", src)
            assert called and called.group(1) in readers, "pipeline_rows_before reads no known index"
            readers[fn.name] = readers[called.group(1)]
    assert {"query_runs_by_date", "query_runs_by_pipeline", "query_by_date_raw",
            "pipeline_rows_before"} <= readers.keys(), readers
    return readers


def _module_constants(tree: ast.Module) -> Dict[str, ast.expr]:
    return {t.id: node.value for node in tree.body if isinstance(node, ast.Assign)
            for t in node.targets if isinstance(t, ast.Name)}


def _resolve(value: Optional[ast.expr], constants: Dict[str, ast.expr]) -> object:
    if isinstance(value, ast.Name) and value.id in constants:
        value = constants[value.id]
    return ast.literal_eval(value) if value is not None else None


def _reader_name(call: ast.Call) -> Optional[str]:
    func = call.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "executions_repo":
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _projections() -> List[Tuple[str, str, str, Dict[str, str]]]:
    """(where, index, ProjectionExpression, ExpressionAttributeNames) of every index read."""
    readers = _reader_indexes()
    found = []
    for path in sorted(CONSOLE_API.rglob("*.py")):
        rel = path.relative_to(CONSOLE_API)
        if rel.parts[0] in ("tests", "ee") or rel.name == "executions_repo.py":
            continue
        tree = ast.parse(path.read_text())
        constants = _module_constants(tree)
        for fn in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
            dicts = {t.id: node.value for node in ast.walk(fn) if isinstance(node, ast.Assign)
                     and isinstance(node.value, ast.Dict) for t in node.targets if isinstance(t, ast.Name)}
            for call in (n for n in ast.walk(fn) if isinstance(n, ast.Call)):
                reader = _reader_name(call)
                if reader not in readers:
                    continue
                where = f"{rel}:{call.lineno}"
                kwargs = {k.arg: k.value for k in call.keywords if k.arg}
                params = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
                passed_through = (isinstance(kwargs.get("projection"), ast.Name)
                                  and kwargs["projection"].id in params)  # its callers are checked
                if "projection" in kwargs and not passed_through:
                    found.append((where, readers[reader], str(_resolve(kwargs["projection"], constants)),
                                  _resolve(kwargs.get("expr_names"), constants) or {}))
                for spread in (k.value for k in call.keywords if k.arg is None):
                    query = dicts.get(spread.id) if isinstance(spread, ast.Name) else None
                    if query is None:
                        continue
                    fields = {key.value: value for key, value in zip(query.keys, query.values)
                              if isinstance(key, ast.Constant)}
                    if "ProjectionExpression" in fields:
                        index = _resolve(fields.get("IndexName"), constants) or readers[reader]
                        found.append((where, str(index), str(_resolve(fields["ProjectionExpression"], constants)),
                                      _resolve(fields.get("ExpressionAttributeNames"), constants) or {}))
    return found


def test_every_index_read_names_only_what_the_index_projects():
    indexes = _index_attributes()
    reads = _projections()
    assert reads, "found no index reads with a projection — the scan is broken"
    reserved = ReservedKeywords.get_reserved_keywords()
    problems = []
    for where, index, projection, names in reads:
        allowed = indexes.get(index, set())
        if index not in indexes:
            problems.append(f"{where}: {index} is not an index of pipeline-tokens")
            continue
        for token in (t.strip() for t in projection.split(",")):
            attribute = names.get(token, token)
            if attribute.startswith("#"):
                problems.append(f"{where}: placeholder {token} has no ExpressionAttributeNames entry")
            elif token.upper() in reserved:
                problems.append(f"{where}: {token} is a DynamoDB reserved word; name it through a placeholder")
            elif allowed is not None and attribute not in allowed:
                problems.append(f"{where}: {index} does not project {attribute}")
    assert not problems, "\n".join(problems)


def test_the_tasks_feed_back_fills_what_its_indexes_lack():
    """/api/tasks reads both indexes without a projection; what they lack comes from the
    base table (_TASKS_BACKFILL). Every row field the feed formats must come from one."""
    import sys
    sys.path.insert(0, str(CONSOLE_API))
    try:
        from routes.tasks import _TASKS_BACKFILL
    finally:
        sys.path.remove(str(CONSOLE_API))
    indexes = _index_attributes()
    formatted = {"execution_name", "task_name", "pipeline_name", "status", "date", "started_at",
                 "running_at", "finished_at", "dependencies", "pipeline_execution", "wait_for",
                 "wrapper_execution_arn", "triggered_by", "attempt", "retry_attempt", "task_config"}
    for index in ("date-pipeline-index", "pipeline-date-index"):
        missing = formatted - (indexes[index] or formatted) - set(_TASKS_BACKFILL)
        assert not missing, f"{index}: {sorted(missing)} neither projected nor back-filled"
