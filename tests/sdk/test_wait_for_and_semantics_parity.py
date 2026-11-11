"""Parity test for the `wait_for_assets` / `assets_ready_set` DynamoDB field
names introduced in v1.1.4 (ADR #129) for correct AND semantics.

Three actors read and write these two StringSet fields across five source
files. A rename in any single place causes the gate to silently stop working:

  Writer                      Field              Reader(s)
  --------------------------  -----------------  --------------------------
  check_assets DAL            wait_for_assets    notify_asset_subscribers
                              assets_ready_set   (via add_to_assets_ready_set)
  notify_asset_subscribers    assets_ready_set   notify_asset_subscribers index,
    DAL (ADD)                                    evaluate_deps, notify_dependents SFN
  notify_dependents SFN       wait_for_assets    evaluate_deps (via event payload)
    (from DDB SS)             assets_ready_set   (via event payload)

This test fails if the same literal drifts across any two files, forcing the
edit to be a coordinated multi-file change rather than a silent divergence.
"""
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

LAMBDAS = REPO_ROOT / "sam" / "lambdas"
CHECK_ASSETS_DAL = LAMBDAS / "check_assets" / "dal" / "__init__.py"
NOTIFY_DAL = LAMBDAS / "notify_asset_subscribers" / "dal" / "__init__.py"
NOTIFY_INDEX = LAMBDAS / "notify_asset_subscribers" / "index.py"
EVALUATE_DEPS = LAMBDAS / "evaluate_deps" / "index.py"
NOTIFY_DEPENDENTS_SFN = (
    REPO_ROOT / "sam" / "sfn_templates" / "helpers" / "notify_dependents" / "sfn.tpl.json"
)

FIELD_WAIT_FOR_ASSETS = "wait_for_assets"
FIELD_ASSETS_READY_SET = "assets_ready_set"


@pytest.fixture(scope="module")
def check_assets_dal() -> str:
    return CHECK_ASSETS_DAL.read_text()


@pytest.fixture(scope="module")
def notify_dal() -> str:
    return NOTIFY_DAL.read_text()


@pytest.fixture(scope="module")
def notify_index() -> str:
    return NOTIFY_INDEX.read_text()


@pytest.fixture(scope="module")
def evaluate_deps() -> str:
    return EVALUATE_DEPS.read_text()


@pytest.fixture(scope="module")
def notify_dependents_sfn() -> str:
    return NOTIFY_DEPENDENTS_SFN.read_text()


class TestWaitForAssetsField:
    """`wait_for_assets` written by check_assets, read by notify_asset_subscribers
    for the AND-completeness gate and by evaluate_deps for the final verdict."""

    def test_written_by_check_assets_dal(self, check_assets_dal):
        assert FIELD_WAIT_FOR_ASSETS in check_assets_dal, (
            "check_assets DAL no longer writes 'wait_for_assets' — "
            "notify_asset_subscribers will not find the pending-asset set "
            "and will fall back to OR semantics (first-asset wakes the task)."
        )

    def test_read_by_notify_asset_subscribers_index(self, notify_index):
        assert FIELD_WAIT_FOR_ASSETS in notify_index, (
            "notify_asset_subscribers no longer reads 'wait_for_assets' — "
            "AND-gate check is broken; first asset arriving will signal."
        )

    def test_read_by_evaluate_deps(self, evaluate_deps):
        assert FIELD_WAIT_FOR_ASSETS in evaluate_deps, (
            "evaluate_deps no longer reads 'wait_for_assets' — it will fall "
            "back to the legacy bool path only, losing AND semantics for tasks "
            "whose notify_dependents path goes through evaluate_deps."
        )

    def test_passed_by_notify_dependents_sfn(self, notify_dependents_sfn):
        assert FIELD_WAIT_FOR_ASSETS in notify_dependents_sfn, (
            "notify_dependents SFN template no longer passes 'wait_for_assets' "
            "in the evaluate_deps invocation payload — the AND gate is bypassed "
            "for tasks woken via task_dep completion."
        )


class TestAssetsReadySetField:
    """`assets_ready_set` atomically grown by notify_asset_subscribers DAL,
    then read by both the index (AND-gate) and evaluate_deps (final verdict)."""

    def test_written_by_notify_dal(self, notify_dal):
        assert FIELD_ASSETS_READY_SET in notify_dal, (
            "notify_asset_subscribers DAL no longer writes 'assets_ready_set' — "
            "the ADD-and-get call won't accumulate arriving assets; AND gate breaks."
        )

    def test_read_by_notify_asset_subscribers_index(self, notify_index):
        assert FIELD_ASSETS_READY_SET in notify_index, (
            "notify_asset_subscribers index no longer reads 'assets_ready_set' — "
            "cannot compare against wait_for_assets; AND gate will not hold."
        )

    def test_read_by_evaluate_deps(self, evaluate_deps):
        assert FIELD_ASSETS_READY_SET in evaluate_deps, (
            "evaluate_deps no longer reads 'assets_ready_set' — loses the "
            "set-based completeness check; falls back to legacy bool only."
        )

    def test_passed_by_notify_dependents_sfn(self, notify_dependents_sfn):
        assert FIELD_ASSETS_READY_SET in notify_dependents_sfn, (
            "notify_dependents SFN template no longer passes 'assets_ready_set' "
            "to evaluate_deps — AND semantics broken for task_dep-triggered wake-up."
        )
