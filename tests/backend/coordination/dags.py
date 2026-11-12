"""Pipelines for scenarios, built with the real SDK so their ASL comes from the real generator."""

from __future__ import annotations

import warnings
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from world import TASK_ARN

with warnings.catch_warnings():
    warnings.simplefilter("ignore")           # the asset API's ExperimentalWarning
    from polyris import DAG, task
    from polyris.assets import Asset


def asset(name: str, **kwargs: Any) -> Any:
    """An asset whose name is `name` (the SDK derives names from the URI)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return Asset(f"s3://{name}", **kwargs)


def pipeline(dag_id: str, tasks: Sequence[Tuple[str, Dict[str, Any]]],
             edges: Iterable[Tuple[str, str]] = (), *, schedule: Any = "cron(0 6 * * ? *)") -> Any:
    """A pipeline of `@task.sfn` tasks; `tasks` is [(task_id, decorator kwargs)], `edges` [(upstream, downstream)].

    `schedule` is a cron expression, or a list of assets for an asset-triggered pipeline.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with DAG(dag_id, schedule=schedule) as dag:
            made: Dict[str, Any] = {}
            for name, kwargs in tasks:
                def body() -> None:
                    """A user task; the harness finishes it with World.complete."""

                made[name] = task.sfn(arn=TASK_ARN.format(name), task_id=name, **kwargs)(body)()
            for upstream, downstream in edges:
                made[upstream] >> made[downstream]
    return dag


def chain(dag_id: str, *names: str, **kwargs: Any) -> Any:
    """a >> b >> c ..."""
    return pipeline(dag_id, [(n, {}) for n in names], zip(names, names[1:]), **kwargs)


def waits_for(dag_id: str, spec: List[Any], *, deps: Sequence[str] = (), trigger_rule: Optional[str] = None) -> Any:
    """One `consumer` task with `wait_for=spec`, downstream of `deps`."""
    consumer: Dict[str, Any] = {"wait_for": spec}
    if trigger_rule:
        consumer["trigger_rule"] = trigger_rule
    return pipeline(dag_id, [*[(d, {}) for d in deps], ("consumer", consumer)], [(d, "consumer") for d in deps])
