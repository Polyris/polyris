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

    def test_pushed_at_field_appears_in_sfn_template(self, sfn_template_text):
        """`pushed_at` is stamped by xcom.push() and REMOVE'd by
        Init_Output_Row. Not a Python constant but coupled across SDK and
        template — a rename in one without the other breaks marker cleanup
        on same-date re-runs."""
        assert "pushed_at" in sfn_template_text

    def test_push_count_field_appears_in_sfn_template(self, sfn_template_text):
        """`push_count` is ADD'd by xcom.push() and REMOVE'd by
        Init_Output_Row (post-0.100.0 addition to the REMOVE clause).
        Same coupling as pushed_at."""
        assert "push_count" in sfn_template_text


# ── Canonical row-key formats (SDK ↔ SFN template ↔ console_api reader) ──


class TestCanonicalRowKeyFormats:
    """The `output#{pipeline}#{task}#{date}` and `input#{pipeline}#{task}#{date}`
    key formats are hard-coded in four places: SDK reader (`xcom.pull`), SFN
    template (`Init_Output_Row`, `Save_Input_Record`, `Save_Canonical_Output`,
    `Save_Success_Preserve`), console_api writer + reader, and the internal-
    record filter (`is_internal_record`). Any rename in one without the
    others breaks readers silently. Pin the exact prefixes here."""

    def test_output_prefix_in_sdk(self):
        xcom_text = (REPO_ROOT / "polyris" / "xcom.py").read_text()
        assert '"output#"' in xcom_text or "'output#'" in xcom_text or 'f"output#{' in xcom_text

    def test_output_prefix_in_sfn_template(self, sfn_template_text):
        # The template uses JSONata expressions like `'output#' & ...`.
        assert "'output#'" in sfn_template_text

    def test_output_prefix_in_console_api(self, tasks_route_text):
        assert 'f"output#{' in tasks_route_text or "'output#'" in tasks_route_text

    def test_input_prefix_in_sfn_template(self, sfn_template_text):
        assert "'input#'" in sfn_template_text

    def test_input_prefix_in_console_api(self, tasks_route_text):
        assert 'f"input#{' in tasks_route_text or "'input#'" in tasks_route_text

    def test_output_prefix_in_internal_record_filter(self):
        utils_text = (REPO_ROOT / "sam" / "lambdas" / "console_api" / "utils.py").read_text()
        assert "'output#'" in utils_text or '"output#"' in utils_text

    def test_input_prefix_in_internal_record_filter(self):
        # 0.100.0 added the input# filter — regression guard so a future
        # rename of the prefix doesn't leak input rows into All Tasks.
        utils_text = (REPO_ROOT / "sam" / "lambdas" / "console_api" / "utils.py").read_text()
        assert "'input#'" in utils_text or '"input#"' in utils_text


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
        xcom._MANUAL_PIPELINE_EXECUTION_FIELD,
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


# ── Runtime env-var injection per service-task branch ─────────────────────


class TestServiceTaskEnvInjectionParity:
    """``xcom.push()`` from Glue / ECS / Batch reads the wrapper-injected
    ``POLYRIS_TASK_NAME`` and ``POLYRIS_WRAPPER_RUN_ID`` env vars (defined
    as constants in ``polyris/xcom.py``: ``ENV_TASK_NAME`` / ``ENV_RUN_ID``).
    The wrapper injects them per service-task branch — Glue via
    ``--Arguments``, ECS/Batch via ``ContainerOverrides.Environment``. A
    missing injection in any single branch silently breaks ``xcom.push()``
    for that task type — the SDK would raise the "requires the env
    variable" error at runtime with no build-time signal.

    Pinning per-branch presence here so a template edit that drops the
    injection from one branch fails the build."""

    SERVICE_BRANCHES = ("Run_Task_Glue", "Run_Task_ECS", "Run_Task_Batch")

    @pytest.fixture(scope="class")
    def branch_bodies(self, sfn_template_text):
        """Extract the JSON body of each service-task state so per-branch
        asserts don't false-positive on a mention in a sibling state."""
        import re
        out = {}
        for name in self.SERVICE_BRANCHES:
            # Match `"Name": {` ... balanced-brace body. The template is
            # small enough (~1200 lines) that a naive regex-with-brace-count
            # via re.finditer over `{` and `}` is fine.
            start_match = re.search(rf'"{re.escape(name)}"\s*:\s*\{{', sfn_template_text)
            assert start_match, f"State {name!r} not found in run_task template"
            start = start_match.end() - 1  # position of the opening brace
            depth = 0
            for i in range(start, len(sfn_template_text)):
                ch = sfn_template_text[i]
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        out[name] = sfn_template_text[start:i + 1]
                        break
            else:
                raise AssertionError(f"State {name!r} — unmatched braces")
        return out

    @pytest.mark.parametrize("branch", SERVICE_BRANCHES)
    def test_branch_injects_task_name_env(self, branch, branch_bodies):
        # SDK reads xcom.ENV_TASK_NAME = "POLYRIS_TASK_NAME"; the string
        # must appear inside this state's body verbatim (as a JSON key for
        # Glue Arguments, or a Name value for ECS/Batch Environment entries).
        assert xcom.ENV_TASK_NAME in branch_bodies[branch], (
            f"{branch} does not inject {xcom.ENV_TASK_NAME!r} — xcom.push() "
            f"in this task type will fail with 'requires the env variable' "
            "at runtime."
        )

    @pytest.mark.parametrize("branch", SERVICE_BRANCHES)
    def test_branch_injects_wrapper_run_id_env(self, branch, branch_bodies):
        assert xcom.ENV_RUN_ID in branch_bodies[branch], (
            f"{branch} does not inject {xcom.ENV_RUN_ID!r} — xcom.push() "
            f"in this task type will fail with 'requires the env variable' "
            "at runtime."
        )


class TestEmrPushDeliberatelyUnsupported:
    """Negative-case guard for `xcom.push()` on EMR steps. CHANGELOG "Known
    limitations" and `docs/features/DATA_PASSING.md` name EMR as unsupported
    in 0.100.0 — `HadoopJarStep.Args` risks breaking arbitrary Spark arg
    parsers, so `POLYRIS_TASK_NAME` / `POLYRIS_WRAPPER_RUN_ID` are NOT
    injected on the EMR branch. If someone adds an injection without wiring
    the push contract end-to-end, this test fails loudly and points at the
    limitations doc — silent enablement would trap the next EMR user with
    a partially-working push feature."""

    @pytest.fixture(scope="class")
    def emr_branch_body(self, sfn_template_text):
        # Reuse TestServiceTaskEnvInjectionParity's brace-count trick.
        import re
        start_match = re.search(r'"Run_Task_EMR"\s*:\s*\{', sfn_template_text)
        assert start_match, "Run_Task_EMR state not found in run_task template"
        start = start_match.end() - 1
        depth = 0
        for i in range(start, len(sfn_template_text)):
            if sfn_template_text[i] == '{':
                depth += 1
            elif sfn_template_text[i] == '}':
                depth -= 1
                if depth == 0:
                    return sfn_template_text[start:i + 1]
        raise AssertionError("Run_Task_EMR — unmatched braces")

    def test_emr_does_not_inject_task_name_env(self, emr_branch_body):
        assert xcom.ENV_TASK_NAME not in emr_branch_body, (
            f"Run_Task_EMR injects {xcom.ENV_TASK_NAME!r}. If push support "
            "is now intended, wire the whole contract (docs + IAM matrix + "
            "CHANGELOG 'Known limitations') and delete this test."
        )

    def test_emr_does_not_inject_wrapper_run_id_env(self, emr_branch_body):
        assert xcom.ENV_RUN_ID not in emr_branch_body, (
            f"Run_Task_EMR injects {xcom.ENV_RUN_ID!r}. Same as above — "
            "either finish the push contract or don't inject the env at all."
        )
