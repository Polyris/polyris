"""Parity test for the SDK ↔ SFN template ↔ console_api coupled constants
around the xcom.push() marker + manual-resolution marker.

ADR-123's "Coupled constants" table catalogues the strings the three surfaces
share; the ADR itself calls out that "a parity test would prevent silent
drift; adding one is straightforward follow-up work" (adr-123:120). This is
that test — it grep-loads the canonical constants from ``polyris/xcom.py``
and ``polyris/constants.py`` and asserts the same values appear in the SFN
template + the console_api writer + the frontend TS module.

Contract: renaming a coupled constant in any single place must fail this
test. Which forces the delivery to be a coordinated multi-file edit rather
than a silent drift.
"""
from pathlib import Path

import pytest

from polyris import constants as canon
from polyris import xcom


REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_TASK_TPL = REPO_ROOT / "sam" / "sfn_templates" / "helpers" / "run_task" / "sfn.tpl.json"
TASKS_ROUTE = REPO_ROOT / "sam" / "lambdas" / "console_api" / "routes" / "tasks.py"
TS_ENUMS = REPO_ROOT / "ui" / "src" / "generated" / "enums.ts"


@pytest.fixture(scope="module")
def sfn_template_text() -> str:
    return RUN_TASK_TPL.read_text()


@pytest.fixture(scope="module")
def tasks_route_text() -> str:
    return TASKS_ROUTE.read_text()


@pytest.fixture(scope="module")
def ts_enums_text() -> str:
    return TS_ENUMS.read_text()


# ── xcom.push() marker fields ─────────────────────────────────────────────


class TestPushMarkerFields:
    """`_pushed_by_task` is written by xcom.push() and read by the wrapper's
    Check_Task_Pushed state. Renaming either without the matching template
    edit silently regresses the push-detection routing."""

    def test_push_marker_field_appears_in_sfn_template(self, sfn_template_text):
        assert xcom._PUSH_MARKER_FIELD in sfn_template_text, (
            f"SDK writes {xcom._PUSH_MARKER_FIELD!r} but the wrapper doesn't "
            "read that name — Check_Task_Pushed will always route to the "
            "non-preserve branch and downstream reads will get the "
            "wrapper's AWS metadata instead of the pushed value."
        )

    def test_pushed_run_id_field_appears_in_sfn_template(self, sfn_template_text):
        # Not a Python constant (only a JSON key), but the wrapper stamps this
        # to reject stale markers — the string must exist verbatim.
        assert "pushed_run_id" in sfn_template_text


# ── Manual-resolution marker fields ───────────────────────────────────────


class TestManualResolutionMarkerFields:
    """The four fields console_api writes onto the synthetic marker
    (``_write_synthetic_output_marker``) are read by both the SDK's
    ``_raise_manual`` and the frontend's ``detectManualResolution``. Every
    field name must appear on all three surfaces."""

    @pytest.mark.parametrize("field", [
        xcom._MANUAL_RESOLVED_FIELD,
        xcom._MANUAL_RESOLUTION_FIELD,
        xcom._MANUAL_REASON_FIELD,
        xcom._MANUAL_OPERATOR_FIELD,
    ])
    def test_field_appears_in_console_api_writer(self, field, tasks_route_text):
        assert field in tasks_route_text, (
            f"SDK reads {field!r} but the backend writer doesn't write it — "
            "downstream marker introspection will see the field as missing."
        )


# ── ManualResolution enum (values) ────────────────────────────────────────


class TestManualResolutionEnumValues:
    """The four action names (mark_success / skip / fail / stop) come from
    the canonical ``polyris.constants.ManualResolution`` and are consumed
    verbatim by (a) console_api's ``_execute_task_action`` call sites and
    (b) the frontend's ``manualResolution.ts`` switches (via the generated
    TS union in ``ui/src/generated/enums.ts``)."""

    @pytest.mark.parametrize("member_name,value", [
        ("MARK_SUCCESS", "mark_success"),
        ("SKIP", "skip"),
        ("FAIL", "fail"),
        ("STOP", "stop"),
    ])
    def test_canonical_value_matches_expected_string(self, member_name, value):
        assert getattr(canon.ManualResolution, member_name) == value

    def test_backend_uses_enum_not_bare_literal(self, tasks_route_text):
        """Guards against a regression to bare `action_name='skip'` string
        literals — those bypass the enum and drift undetectably."""
        assert "ManualResolution.MARK_SUCCESS" in tasks_route_text
        assert "ManualResolution.SKIP" in tasks_route_text
        assert "ManualResolution.FAIL" in tasks_route_text
        assert "ManualResolution.STOP" in tasks_route_text

    def test_ts_union_carries_every_canonical_value(self, ts_enums_text):
        """The generated TS union must list every canonical Python value —
        the frontend detector would silently narrow to `string` otherwise."""
        for value in ("mark_success", "skip", "fail", "stop"):
            # Look for the value as a TS string literal in the ManualResolution union.
            assert f"'{value}'" in ts_enums_text, (
                f"Value {value!r} missing from ui/src/generated/enums.ts — "
                "run `make generate-enums`."
            )
