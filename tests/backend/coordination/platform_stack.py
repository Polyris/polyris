"""The platform stack from sam/template.yaml, loaded into the harness interpreter.

Every DynamoDB table becomes a moto table with its declared keys and
indexes. Every state machine with a DefinitionUri is loaded with its
DefinitionSubstitutions resolved the way CloudFormation resolves them —
table names from `!Ref`, function and machine ARNs from `!GetAtt`/`!Ref` —
so a template wired to the wrong table writes to the wrong moto table and
the scenario fails, as it would in AWS.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, Optional

from coordination_testkit import REPO_ROOT, template
from sfn_runtime import ACCOUNT, REGION, Runtime

PARAMETERS = {"Namespace": "acme", "Stage": "dev", "AWS::Region": REGION, "AwsRegion": REGION,
              "AWS::AccountId": ACCOUNT, "AWS::Partition": "aws"}
MACHINE_TYPES = ("AWS::Serverless::StateMachine", "AWS::StepFunctions::StateMachine")
FUNCTION_TYPES = ("AWS::Serverless::Function", "AWS::Lambda::Function")


class Stack:
    def __init__(self, ddb_client: Any, *, now: Callable[[], str]):
        loaded = template()
        self.resources: Dict[str, Any] = loaded["Resources"]
        # Template parameters at their defaults, with the harness's names on top.
        self.parameters = {k: v.get("Default") for k, v in loaded.get("Parameters", {}).items()}
        self.parameters.update(PARAMETERS)
        self.ddb = ddb_client
        self.tables = {logical: self._sub(r["Properties"]["TableName"])
                       for logical, r in self.resources.items() if r["Type"] == "AWS::DynamoDB::Table"}
        self.functions = {logical: self._sub(r["Properties"]["FunctionName"])
                          for logical, r in self.resources.items()
                          if r["Type"] in FUNCTION_TYPES and "FunctionName" in r["Properties"]}
        self.machine_names = {logical: self._sub(r["Properties"]["Name"])
                              for logical, r in self.resources.items()
                              if r["Type"] in MACHINE_TYPES and "Name" in r["Properties"]}
        self.runtime = Runtime(ddb_client, now=now)
        self._create_tables()
        for logical in self.machine_names:
            props = self.resources[logical]["Properties"]
            if "DefinitionUri" in props:
                self.runtime.add_machine(self.machine_names[logical], self._definition(logical),
                                         type=props.get("Type", "STANDARD"))

    # ── names ──────────────────────────────────────────────────────────

    def machine_arn(self, logical: str) -> str:
        return f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:{self.machine_names[logical]}"

    def function_arn(self, logical: str) -> str:
        return f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{self.functions[logical]}"

    def bind_lambda(self, logical: str, handler: Callable[[Any, Any], Any]) -> None:
        """Serve `logical`'s invocations with an in-process handler."""
        self.runtime.lambdas[self.functions[logical]] = handler

    # ── CloudFormation resolution ──────────────────────────────────────

    def _resolve(self, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        if "!Ref" in value:
            return self._ref(value["!Ref"])
        if "!GetAtt" in value:
            return self._get_att(value["!GetAtt"])
        if "!Sub" in value:
            return self._sub(value)
        raise NotImplementedError(f"intrinsic {value}")

    def _ref(self, name: str) -> Any:
        if name in self.parameters:
            return self.parameters[name]
        kind = self.resources[name]["Type"]
        if kind == "AWS::DynamoDB::Table":
            return self.tables[name]
        if kind in MACHINE_TYPES:
            return self.machine_arn(name)
        if kind in FUNCTION_TYPES:
            return self.functions[name]
        # Resources the harness does not interpret (an HTTP API id, a bucket name, ...).
        return f"ref-{name}"

    def _get_att(self, target: str) -> str:
        name, attribute = target.split(".", 1)
        kind = self.resources[name]["Type"]
        if attribute == "Arn" and kind in FUNCTION_TYPES:
            return self.function_arn(name)
        if attribute == "Arn" and kind == "AWS::DynamoDB::Table":
            return f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{self.tables[name]}"
        if attribute in ("Arn", "Name") and kind in MACHINE_TYPES:
            return self.machine_arn(name) if attribute == "Arn" else self.machine_names[name]
        raise NotImplementedError(f"!GetAtt {target}")

    def _sub(self, value: Any) -> str:
        text = value["!Sub"] if isinstance(value, dict) else value

        def one(match: re.Match) -> str:
            key = match.group(1)
            if "." in key and key not in self.parameters:
                return str(self._get_att(key))
            return str(self._ref(key))

        return re.sub(r"\$\{([^}]+)\}", one, text)

    # ── loading ────────────────────────────────────────────────────────

    def _definition(self, logical: str) -> Dict[str, Any]:
        props = self.resources[logical]["Properties"]
        text = (REPO_ROOT / "sam" / props["DefinitionUri"]).read_text()
        values = {k: self._resolve(v) for k, v in (props.get("DefinitionSubstitutions") or {}).items()}
        missing = sorted(set(re.findall(r"\$\{(\w+)\}", text)) - set(values))
        if missing:
            raise AssertionError(f"{logical} uses substitutions that template.yaml does not define: {missing}")
        return json.loads(re.sub(r"\$\{(\w+)\}", lambda m: str(values[m.group(1)]), text))

    def _create_tables(self) -> None:
        for logical, name in self.tables.items():
            props = self.resources[logical]["Properties"]
            kwargs: Dict[str, Any] = {"TableName": name, "KeySchema": props["KeySchema"],
                                      "AttributeDefinitions": props["AttributeDefinitions"],
                                      "BillingMode": "PAY_PER_REQUEST"}
            if props.get("GlobalSecondaryIndexes"):
                kwargs["GlobalSecondaryIndexes"] = [{k: g[k] for k in ("IndexName", "KeySchema", "Projection")}
                                                    for g in props["GlobalSecondaryIndexes"]]
            self.ddb.create_table(**kwargs)

    # ── pipelines ──────────────────────────────────────────────────────

    def deploy_pipeline(self, dag: Any, *, name: Optional[str] = None) -> str:
        """Generate the pipeline's ASL with the real generator and load it; returns its ARN."""
        from polyris.generators import generate_step_function_json

        asl = generate_step_function_json(
            dag,
            wrapper_arn=self.machine_arn("DependencyWrapperSfn"),
            registry_table=self.tables["PipelineRegistryTable"],
            tokens_table=self.tables["PipelineTokensTable"],
            asset_subscriptions_table=self.tables["AssetSubscriptionsTable"],
        )
        missing = re.findall(r"\$\{(\w+)\}", asl)
        if missing:
            raise AssertionError(f"generated ASL for {dag.dag_id} still has placeholders: {sorted(set(missing))}")
        return self.runtime.add_machine(name or f"acme-dev-{dag.dag_id}", json.loads(asl)).arn
