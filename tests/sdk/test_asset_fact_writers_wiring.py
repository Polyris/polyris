"""Template contract: both platform writers of asset facts are wired alike (ADR #130 §1).

run_task records facts in its Record_Asset_Event state; the console records
them for Mark Successful through asset_events_repo.write_fact. Waiting tasks
are woken by the asset-events stream, so neither writer calls a notifier —
but both must be able to write the table, and their facts must live equally
long, or a manual fact expires while an equal natural one still serves.
"""

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent.parent
TEMPLATE_PATH = REPO_ROOT / "sam" / "template.yaml"


@pytest.fixture(scope="module")
def resources() -> dict:
    class Loader(yaml.SafeLoader):
        pass

    def intrinsic(loader, suffix, node):
        if isinstance(node, yaml.ScalarNode):
            return {f"!{suffix}": loader.construct_scalar(node)}
        if isinstance(node, yaml.SequenceNode):
            return {f"!{suffix}": loader.construct_sequence(node, deep=True)}
        return {f"!{suffix}": loader.construct_mapping(node, deep=True)}

    Loader.add_multi_constructor("!", intrinsic)
    return yaml.load(TEMPLATE_PATH.read_text(), Loader=Loader)["Resources"]


def test_both_writers_keep_facts_equally_long(resources):
    console = resources["ConsoleApiFunction"]["Properties"]["Environment"]["Variables"]
    run_task = resources["RunTaskHelperSfn"]["Properties"]["DefinitionSubstitutions"]
    assert int(console["ASSET_EVENT_TTL_DAYS"]) == int(run_task["asset_event_ttl_days"])


def test_both_writers_write_the_same_table(resources):
    console = resources["ConsoleApiFunction"]["Properties"]["Environment"]["Variables"]
    run_task = resources["RunTaskHelperSfn"]["Properties"]["DefinitionSubstitutions"]
    assert console["ASSET_EVENTS_TABLE"] == run_task["asset_events_table"] == {"!Ref": "AssetEventsTable"}


def test_the_console_role_may_write_facts(resources):
    statements = resources["ConsoleApiRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    assert any("dynamodb:PutItem" in s["Action"] and {"!GetAtt": "AssetEventsTable.Arn"} in s["Resource"]
               for s in statements)


def test_no_writer_invokes_a_notifier(resources):
    console = resources["ConsoleApiFunction"]["Properties"]["Environment"]["Variables"]
    run_task = resources["RunTaskHelperSfn"]["Properties"]["DefinitionSubstitutions"]
    assert "NOTIFY_ASSET_SUBSCRIBERS_LAMBDA_ARN" not in console
    assert "notify_asset_subscribers_lambda_arn" not in run_task
