"""A spec version as polyris-deploy builds it, and the check every reader makes (ADR #137 §8)."""

import pytest

from polyris.coordination import task_spec
from polyris.coordination.task_spec import SK, VERSION_HASH, SpecError


def _spec(task_id, **extra):
    return {"task_id": task_id, "task_type": "sfn", "dependencies": [], "trigger_rule": "all_success", **extra}


def _read_back(version):
    """The version's items and texts as a reader gets them."""
    texts = {item[SK]: item[task_spec.TEXT] for item in version.text_items()}
    return version.version_items(), texts


def test_a_version_names_its_tasks_texts_and_stores_a_shared_text_once():
    version = task_spec.build_version("orders", [_spec("extract"), _spec("load")])
    assert version.task_count == 2 and version.spec_id == f"orders#{version.fingerprint}"
    same = task_spec.build_version("orders", [_spec("a"), _spec("b", wait_before=1)])
    assert len(same.text_items()) == 2
    twins = task_spec.Version("orders", {"a": "{}", "b": "{}"})
    assert len(twins.text_items()) == 1 and len(twins.version_items()) == 2


def test_a_version_cannot_name_a_task_twice():
    with pytest.raises(ValueError, match="names a task twice: 'load'"):
        task_spec.build_version("orders", [_spec("load"), _spec("load")])


def test_a_version_read_back_whole_passes_the_check():
    version = task_spec.build_version("orders", [_spec("extract"), _spec("load", dependencies=["extract"])])
    items, texts = _read_back(version)
    specs = task_spec.check_version(version.spec_id, items, texts, expected_count=2)
    assert {k: v.fields for k, v in specs.items()} == {"extract": _spec("extract"),
                                                      "load": _spec("load", dependencies=["extract"])}


def test_a_version_read_back_wrong_is_refused_with_the_cause():
    version = task_spec.build_version("orders", [_spec("extract"), _spec("load")])
    items, texts = _read_back(version)
    with pytest.raises(SpecError, match="without a task_id or a text hash"):
        task_spec.check_version(version.spec_id, items + [{SK: "x"}], texts)
    with pytest.raises(SpecError, match="names a task twice"):
        task_spec.check_version(version.spec_id, items + [items[0]], texts)
    swapped = [{**items[0], VERSION_HASH: items[1][VERSION_HASH]}, {**items[1], VERSION_HASH: items[0][VERSION_HASH]}]
    forged = f"orders#{task_spec.fingerprint((i[SK], i[VERSION_HASH]) for i in swapped)}"
    with pytest.raises(SpecError, match="names the spec of"):
        task_spec.check_version(forged, swapped, texts)


@pytest.mark.parametrize("registered,needs", [
    (None, True), ("one", True), (True, True), (0, True), (task_spec.FORMAT_VERSION, False),
    (str(task_spec.FORMAT_VERSION), False), (task_spec.FORMAT_VERSION + 1, False),
])
def test_a_pipeline_needs_a_redeploy_without_this_format_or_a_later_one(registered, needs):
    assert task_spec.needs_redeploy(registered) is needs
