"""Who tells a pipeline run how its tasks ended (ADR #135, ADR #137 §4).

In 1.3 the dispatcher answers a run once every row is final, and no template
answers one: run_task answers none. The 1.2 wrapper and its failure handler,
which answered a run's token from six places, are gone (TR-8). A template state
that sends a task token's success or failure would be a second answerer, the
class ADR #135 closes, so it fails here.
"""

from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[2] / "sam" / "sfn_templates"
GENERATORS = Path(__file__).resolve().parents[2] / "polyris" / "generators.py"


def test_no_template_answers_a_run():
    """A new state that answers a token fails here until ADR #137 §4 says who answers that run."""
    sources = [*TEMPLATES.rglob("*.tpl.json"), GENERATORS]
    answering = [str(p) for p in sources if "sendTaskSuccess" in p.read_text() or "sendTaskFailure" in p.read_text()]
    assert answering == []


def test_no_template_marks_a_failure_as_answered_already():
    """helper_failed told the failure handler that run_task had answered the run; twice it had not (F16, B1)."""
    assert [p.name for p in TEMPLATES.rglob("*.tpl.json") if "helper_failed" in p.read_text()] == []
