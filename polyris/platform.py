"""Platform release contract and the pure logic behind ``polyris-up`` / ``polyris-down`` (ADR #131).

Two producers and one consumer share the names in this module:

- ``scripts/build_platform_release.py`` (CI) builds the release archive:
  ``manifest.json``, a JSON template, one zip per Lambda, the Step Functions
  definitions and the prebuilt UI;
- ``polyris/up.py`` (``polyris-up``) downloads that archive, uploads it to the
  user's artifacts bucket, rewrites the template to point at the uploads and
  deploys it.

Everything here is pure (no AWS calls), so it sits under the coverage floor.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

# ── Release archive contract ─────────────────────────────────────────────────

RELEASE_REPO = "Polyris/polyris"
MANIFEST_NAME = "manifest.json"
# Bump when the archive layout changes in a way an older polyris-up can't read.
MANIFEST_FORMAT = 1


def release_asset_name(version: str) -> str:
    """File name of the platform archive attached to the GitHub Release of ``v{version}``."""
    return f"polyris-platform-v{version}.zip"


def release_asset_url(version: str) -> str:
    """Download URL of the platform archive for ``version`` (no leading ``v``)."""
    return f"https://github.com/{RELEASE_REPO}/releases/download/v{version}/{release_asset_name(version)}"


# ── Stack identity ───────────────────────────────────────────────────────────

# Stack-level tag that marks a platform stack. The value is constant on
# purpose: stack tags propagate to every taggable resource, so a per-version
# value would touch every resource on every upgrade. The version lives in the
# stack Description instead (see DESCRIPTION_PREFIX).
PLATFORM_TAG_KEY = "polyris:platform"
PLATFORM_TAG_VALUE = "true"
DESCRIPTION_PREFIX = "Polyris platform v"

CAPABILITIES = ("CAPABILITY_IAM", "CAPABILITY_NAMED_IAM", "CAPABILITY_AUTO_EXPAND")

# Template parameter names polyris-up reads or sets. Pinned against
# sam/template.yaml by tests/sdk/test_platform.py.
PARAM_NAMESPACE = "Namespace"
PARAM_STAGE = "Stage"
PARAM_ENABLE_COGNITO = "EnableCognitoAuth"
PARAM_ADMIN_EMAIL = "AdminEmail"
PARAM_ARTIFACTS_BUCKET = "ArtifactsBucket"
PARAM_UI_PREFIX = "UiArtifactsPrefix"

# Template outputs polyris-up / polyris-init read.
OUTPUT_CONSOLE_URL = "ConsoleUiUrl"
# ARN of the built-in test Step Function (succeeds after ~1 s); the target of
# `polyris-init <name> --hello`.
OUTPUT_TEST_SFN_ARN = "TestQuickSfnArn"

# AWS caps IAM role names at 64 characters. The longest role in the template
# is "{Namespace}-{Stage}-polyris-notify-asset-subscribers-role" (39 fixed
# characters), so Namespace + Stage may use at most 25.
NAMESPACE_STAGE_MAX_LEN = 25

# S3 bucket names allow lowercase letters, digits and hyphens here; both parts
# end up inside "{Namespace}-{Stage}-polyris-*" bucket names.
_NAME_PART_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")


def default_stack_name(namespace: str, stage: str) -> str:
    """Name of a new platform stack."""
    return f"{namespace}-{stage}"


def platform_bucket_names(namespace: str, stage: str) -> Tuple[str, str]:
    """Globally unique bucket names the template creates (results, console UI)."""
    prefix = f"{namespace}-{stage}-polyris"
    return f"{prefix}-results", f"{prefix}-console-ui"


def artifacts_bucket_name(account_id: str, region: str) -> str:
    """Per-account, per-region bucket polyris-up uploads release artifacts to."""
    return f"polyris-artifacts-{account_id}-{region}"


def version_from_description(description: Optional[str]) -> Optional[str]:
    """The platform version recorded in a stack Description, or None."""
    if not description or not description.startswith(DESCRIPTION_PREFIX):
        return None
    rest = description[len(DESCRIPTION_PREFIX):]
    return rest.split()[0] if rest.split() else None


def validate_namespace_stage(namespace: str, stage: str) -> List[str]:
    """Return every problem with a Namespace/Stage pair (empty list = valid)."""
    errors: List[str] = []
    for label, value in (("Namespace", namespace), ("Stage", stage)):
        if not _NAME_PART_RE.match(value or ""):
            errors.append(
                f"{label} {value!r} must use only lowercase letters, digits and hyphens, "
                f"and start and end with a letter or digit (it becomes part of S3 bucket names)."
            )
    total = len(namespace or "") + len(stage or "")
    if total > NAMESPACE_STAGE_MAX_LEN:
        errors.append(
            f"Namespace + Stage is {total} characters; the limit is {NAMESPACE_STAGE_MAX_LEN} "
            f"(IAM role names would exceed 64 characters)."
        )
    return errors


class AmbiguousPlatformStack(Exception):
    """More than one platform stack in the region and none was named."""

    def __init__(self, names: List[str]):
        super().__init__(", ".join(names))
        self.names = names


def is_platform_stack(stack: Mapping[str, Any]) -> bool:
    """True when a describe_stacks entry carries the platform tag."""
    return any(
        t.get("Key") == PLATFORM_TAG_KEY and t.get("Value") == PLATFORM_TAG_VALUE
        for t in stack.get("Tags") or []
    )


def is_platform_shaped(stack: Mapping[str, Any]) -> bool:
    """True for a Polyris platform stack of any origin: tagged by polyris-up, or deployed
    from source with `sam deploy` (recognised by the platform's own parameters).

    Guards every command that changes or deletes a stack it was pointed at by
    name, so `--stack my-app` can't adopt or delete an unrelated stack.
    """
    if is_platform_stack(stack):
        return True
    params = {p.get("ParameterKey") for p in stack.get("Parameters") or []}
    return {PARAM_NAMESPACE, PARAM_STAGE, PARAM_ENABLE_COGNITO} <= params


def select_platform_stack(stacks: Iterable[Mapping[str, Any]]) -> Optional[str]:
    """Pick the single platform stack from describe_stacks entries.

    Returns None when there is none. Never guesses: two or more raise
    AmbiguousPlatformStack, and the caller asks for an explicit name.
    """
    names = sorted(s["StackName"] for s in stacks if is_platform_stack(s))
    if len(names) > 1:
        raise AmbiguousPlatformStack(names)
    return names[0] if names else None


# ── Manifest ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Manifest:
    """``manifest.json`` at the root of the platform archive.

    ``functions`` maps a template logical id (an ``AWS::Serverless::Function``)
    to the zip of its built code; ``state_machines`` maps a logical id (an
    ``AWS::Serverless::StateMachine``) to its definition file. Paths are
    relative to the archive root.
    """

    version: str
    template: str
    ui: str
    functions: Dict[str, str] = field(default_factory=dict)
    state_machines: Dict[str, str] = field(default_factory=dict)
    format: int = MANIFEST_FORMAT

    def to_dict(self) -> Dict[str, Any]:
        return {
            "format": self.format,
            "version": self.version,
            "template": self.template,
            "ui": self.ui,
            "functions": dict(sorted(self.functions.items())),
            "state_machines": dict(sorted(self.state_machines.items())),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Manifest":
        """Validate deserialized JSON (Principle #37) and build a Manifest."""
        if not isinstance(data, dict):
            raise ValueError(f"manifest.json must be an object, got {type(data).__name__}")
        fmt = data.get("format")
        if fmt != MANIFEST_FORMAT:
            raise ValueError(
                f"Release archive format {fmt!r} is not supported by this polyris "
                f"(expected {MANIFEST_FORMAT}). Install the polyris version that matches the release."
            )
        for key in ("version", "template", "ui"):
            if not isinstance(data.get(key), str) or not data[key]:
                raise ValueError(f"manifest.json: '{key}' must be a non-empty string")
        maps: Dict[str, Dict[str, str]] = {}
        for key in ("functions", "state_machines"):
            value = data.get(key)
            if not isinstance(value, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in value.items()
            ):
                raise ValueError(f"manifest.json: '{key}' must map logical ids to file paths")
            maps[key] = dict(value)
        return cls(
            version=data["version"],
            template=data["template"],
            ui=data["ui"],
            functions=maps["functions"],
            state_machines=maps["state_machines"],
        )


# ── S3 keys ──────────────────────────────────────────────────────────────────


def artifact_key(version: str, kind: str, digest: str, name: str) -> str:
    """S3 key for one uploaded artifact.

    The content digest is part of the key: CloudFormation only updates a
    Lambda's code when its S3 key changes, so an unchanged key would leave a
    rebuilt function running its old code.
    """
    return f"v{version}/{kind}/{digest[:16]}/{name}"


def ui_prefix(version: str, digest: str) -> str:
    """Key prefix for the uploaded UI (passed as the UiArtifactsPrefix parameter)."""
    return f"v{version}/ui-{digest[:16]}"


# ── Template rewrite ─────────────────────────────────────────────────────────


def rewrite_template(
    template: Mapping[str, Any],
    *,
    bucket: str,
    function_keys: Mapping[str, str],
    state_machine_keys: Mapping[str, str],
    version: str,
) -> Dict[str, Any]:
    """Point every Lambda ``CodeUri`` and state machine ``DefinitionUri`` at S3.

    This is what ``aws cloudformation package`` does, keyed by logical id.
    Also stamps the version into the stack Description. Raises ValueError if
    a key names a missing resource, or if any local path is left unmapped —
    deploying a template with a local path fails late and obscurely.
    """
    out = copy.deepcopy(dict(template))
    resources = out.get("Resources") or {}
    for prop, resource_type, keys in (
        ("CodeUri", "AWS::Serverless::Function", function_keys),
        ("DefinitionUri", "AWS::Serverless::StateMachine", state_machine_keys),
    ):
        for logical_id, key in keys.items():
            resource = resources.get(logical_id)
            if not resource or resource.get("Type") != resource_type:
                raise ValueError(f"Template has no {resource_type} named {logical_id!r}")
            resource.setdefault("Properties", {})[prop] = {"Bucket": bucket, "Key": key}
        unmapped = sorted(
            lid for lid, res in resources.items()
            if res.get("Type") == resource_type and isinstance(res.get("Properties", {}).get(prop), str)
        )
        if unmapped:
            raise ValueError(f"No artifact for {prop} of: {', '.join(unmapped)}")
    out["Description"] = f"{DESCRIPTION_PREFIX}{version}"
    return out


# ── Parameters ───────────────────────────────────────────────────────────────


# Parameters --param may not set. Namespace/Stage name every resource (changing
# them on an existing stack replaces all of it) and are validated through
# --namespace/--stage; the artifact locations belong to polyris-up itself.
RESERVED_PARAMS = frozenset({PARAM_NAMESPACE, PARAM_STAGE, PARAM_ARTIFACTS_BUCKET, PARAM_UI_PREFIX})


def parse_param_overrides(items: Iterable[str]) -> Dict[str, str]:
    """Parse repeated ``--param Key=Value`` flags."""
    result: Dict[str, str] = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"--param expects Key=Value, got {item!r}")
        if key.strip() in RESERVED_PARAMS:
            raise ValueError(f"--param can't set {key.strip()}: use --namespace / --stage for a new install; "
                             f"the others are managed by polyris-up")
        result[key.strip()] = value
    return result


def resolve_parameters(
    *,
    template_params: Iterable[str],
    existing: Optional[Mapping[str, str]],
    values: Mapping[str, str],
) -> List[Dict[str, Any]]:
    """Build CloudFormation ``Parameters`` for create or update.

    - ``values`` are what this run sets explicitly (flags, prompts, artifact
      locations). Every key must be a template parameter.
    - On update (``existing`` is the current stack's parameters), every other
      parameter the stack already has keeps its value (``UsePreviousValue``),
      so the stack itself is the only record of the user's choices.
    - Parameters new in this template version and not set are left out, so
      they take their template default.
    """
    known = set(template_params)
    unknown = sorted(set(values) - known)
    if unknown:
        raise ValueError(f"Unknown template parameter(s): {', '.join(unknown)}")
    params: List[Dict[str, Any]] = [
        {"ParameterKey": k, "ParameterValue": v} for k, v in sorted(values.items())
    ]
    for key in sorted(set(existing or {}) & known - set(values)):
        params.append({"ParameterKey": key, "UsePreviousValue": True})
    return params


# ── Change-set guards ────────────────────────────────────────────────────────

# Resources whose replacement or removal destroys user data or access. A
# change set that touches one of these needs explicit confirmation. The admin
# user is here because changing AdminEmail replaces (deletes) the previous one.
DATA_RESOURCE_TYPES = frozenset({
    "AWS::DynamoDB::Table", "AWS::S3::Bucket", "AWS::Cognito::UserPool", "AWS::Cognito::UserPoolUser",
})


def replaced_data_resources(change_set: Mapping[str, Any]) -> List[str]:
    """Data-holding resources a change set would replace or remove, as display strings."""
    risky = []
    for change in change_set.get("Changes", []):
        rc = change.get("ResourceChange", {})
        if rc.get("ResourceType") not in DATA_RESOURCE_TYPES:
            continue
        if rc.get("Action") == "Remove" or rc.get("Replacement") in ("True", "Conditional"):
            risky.append(f"{rc.get('LogicalResourceId')} ({rc.get('ResourceType')}, {rc.get('Action')})")
    return risky


def platform_tags(existing: Optional[Mapping[str, Any]]) -> List[Dict[str, str]]:
    """The stack's current tags with the platform tag set (a change set replaces all stack tags)."""
    tags = [dict(t) for t in (existing or {}).get("Tags", []) if t.get("Key") != PLATFORM_TAG_KEY]
    tags.append({"Key": PLATFORM_TAG_KEY, "Value": PLATFORM_TAG_VALUE})
    return tags


def parameters_of(stack: Mapping[str, Any]) -> Dict[str, str]:
    """A describe_stacks entry's parameters as {key: value}."""
    return {p["ParameterKey"]: p.get("ParameterValue", "") for p in stack.get("Parameters") or []}

