"""``polyris-up`` / ``polyris-down`` — install, upgrade and remove the platform stack (ADR #131).

``polyris-up`` deploys the prebuilt release of the installed polyris version:

1. resolve credentials and region (standard boto3 chain; ``--profile`` /
   ``--region`` override) and print the target account;
2. find the platform stack by tag (or ``--stack``) — update it, or create one;
3. download the release archive (cached under ``~/.cache/polyris/<version>/``);
4. upload Lambda zips, state machine definitions, the UI and the template to
   ``polyris-artifacts-<account>-<region>``;
5. deploy through a CloudFormation change set and print the console URL.

It runs from any directory and writes nothing there. The pure logic lives in
``polyris/platform.py``; this module is the AWS and terminal side.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError, ProfileNotFound, WaiterError

from . import __version__
from ._ansi import BOLD, BOLD_GREEN, CYAN, RED, YELLOW, colorize_parser
from ._ansi import c as _c
from .platform import (
    CAPABILITIES,
    OUTPUT_CONSOLE_URL,
    OUTPUT_TEST_SFN_ARN,
    PARAM_ADMIN_EMAIL,
    PARAM_ARTIFACTS_BUCKET,
    PARAM_ENABLE_COGNITO,
    PARAM_NAMESPACE,
    PARAM_STAGE,
    PARAM_UI_PREFIX,
    PLATFORM_TAG_KEY,
    DEAD_STACK_STATUSES,
    AmbiguousPlatformStack,
    Manifest,
    artifact_key,
    artifacts_bucket_name,
    default_stack_name,
    is_downgrade,
    is_platform_shaped,
    is_platform_stack,
    parameters_of,
    platform_tags,
    replaced_data_resources,
    parse_param_overrides,
    platform_bucket_names,
    release_asset_name,
    release_asset_url,
    resolve_parameters,
    rewrite_template,
    select_platform_stack,
    ui_prefix,
    validate_namespace_stage,
    version_from_description,
)
from .platform import MANIFEST_NAME
from .cfn_changeset import (
    ChangeSetError,
    all_stacks,
    create_change_set,
    describe_stack,
    execute_change_set,
    start_event_watcher,
    wait_for_stack,
)


class UpError(Exception):
    """A user-facing failure: printed as one red line plus hints, exit code 1."""

    def __init__(self, message: str, *hints: str):
        super().__init__(message)
        self.hints = hints


# ── Terminal helpers ─────────────────────────────────────────────────────────


def _is_interactive() -> bool:
    return sys.stdin.isatty()


def _prompt(label: str, default: Optional[str] = None, *, required: bool = True) -> str:
    """Ask for a value. Without a TTY, only a default can answer."""
    if not _is_interactive():
        if default is not None or not required:
            return default or ""
        raise UpError(f"{label} is required, and stdin is not a terminal.",
                      f"Pass it as a flag (see {_c('polyris-up --help', CYAN)}).")
    suffix = f" [{default}]" if default else ""
    while True:
        answer = input(f"  {label}{suffix}: ").strip()
        if answer or default is not None or not required:
            return answer or (default or "")


def _confirm_typed(expected: str, what: str) -> None:
    if not _is_interactive():
        raise UpError(f"{what} needs confirmation, and stdin is not a terminal.", "Pass --yes to skip it.")
    answer = input(f"  Type the stack name ({_c(expected, CYAN)}) to confirm: ").strip()
    if answer != expected:
        raise UpError("Confirmation did not match — nothing was changed.")


def _confirm_yes(question: str) -> None:
    if not _is_interactive():
        raise UpError(f"{question} needs confirmation, and stdin is not a terminal.", "Pass --yes to skip it.")
    if input(f"  {question} [y/N]: ").strip().lower() not in ("y", "yes"):
        raise UpError("Aborted — nothing was changed.")


def _header(text: str) -> None:
    print(f"\n{_c(f'── {text} ' + '─' * max(0, 60 - len(text)), BOLD)}")


# ── AWS session / target ─────────────────────────────────────────────────────


def _session(profile: Optional[str], region: Optional[str]) -> Tuple[boto3.Session, str, str]:
    """Return (session, region, account_id). No region anywhere is an error, never a default."""
    try:
        # Same precedence as polyris-deploy: POLYRIS_REGION, then AWS_REGION
        # (boto3 itself reads only AWS_DEFAULT_REGION), then the profile.
        env_region = os.environ.get("POLYRIS_REGION") or os.environ.get("AWS_REGION")
        session = boto3.Session(profile_name=profile, region_name=region or env_region or None)
    except ProfileNotFound as e:
        raise UpError(str(e), f"List profiles with {_c('aws configure list-profiles', CYAN)}.")
    resolved_region = session.region_name
    if not resolved_region:
        raise UpError(
            "No AWS region configured.",
            "Pass --region, set AWS_REGION, or set a region on the profile.",
        )
    try:
        account = session.client("sts").get_caller_identity()["Account"]
    except NoCredentialsError:
        raise UpError("No AWS credentials found.",
                      f"Pass --profile, set AWS_PROFILE, run {_c('aws configure', CYAN)}, or use AWS CloudShell.")
    except (ClientError, BotoCoreError) as e:
        raise UpError(f"Could not verify AWS credentials: {e}")
    return session, resolved_region, account


def _print_target(*, profile: Optional[str], region: str, account: str) -> None:
    print(f"   profile:  {_c(profile or os.environ.get('AWS_PROFILE') or '(default chain)', CYAN)}")
    print(f"   region:   {_c(region, CYAN)}")
    print(f"   account:  {_c(account, CYAN)}")


def stack_using_namespace(cfn, namespace: str, stage: str) -> Optional[str]:
    """Name of a platform-shaped stack (any origin) deployed with this Namespace/Stage, if any."""
    for stack in all_stacks(cfn):
        params = parameters_of(stack)
        if params.get(PARAM_NAMESPACE) == namespace and params.get(PARAM_STAGE) == stage and is_platform_shaped(stack):
            return str(stack["StackName"])
    return None


def find_platform_stack(
    cfn, explicit: Optional[str], *, include_dead: bool = False, include_review: bool = True,
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Resolve the target stack: (name, existing stack or None).

    With ``--stack`` the name is used as given. Otherwise the single stack
    tagged as a platform is taken (see select_platform_stack for which states
    count); several raise, none returns (None, None).
    """
    if explicit:
        return explicit, describe_stack(cfn, explicit)
    try:
        name = select_platform_stack(all_stacks(cfn), include_dead=include_dead, include_review=include_review)
    except AmbiguousPlatformStack as e:
        raise UpError(
            f"Several Polyris platform stacks in this region: {', '.join(e.names)}.",
            "Pick one with --stack <name>.",
        )
    if name is None:
        return None, None
    return name, describe_stack(cfn, name)


def _warn_dead_platform_stacks(cfn) -> None:
    """Point at platform stacks left over from a failed install or delete (discovery skips them)."""
    dead = sorted(
        (s["StackName"], s.get("StackStatus", "")) for s in all_stacks(cfn)
        if is_platform_stack(s) and s.get("StackStatus", "") in DEAD_STACK_STATUSES
    )
    for name, status in dead:
        print(_c(f"   {name} is {status} (a failed install or delete). Remove it with "
                 f"polyris-down --stack {name}.", YELLOW))


def _has_polyris_change_set(cfn, stack_name: str) -> bool:
    """True when the stack holds a change set created by polyris (named ``polyris-<ms>``)."""
    for page in cfn.get_paginator("list_change_sets").paginate(StackName=stack_name):
        if any(cs.get("ChangeSetName", "").startswith("polyris-") for cs in page.get("Summaries", [])):
            return True
    return False


def hello_task_arn(*, profile: Optional[str], region: Optional[str], stack: Optional[str]) -> Tuple[str, str]:
    """(platform stack name, ARN of its built-in test Step Function) for `polyris-init --hello`."""
    session, _region, _account = _session(profile, region)
    name, existing = find_platform_stack(session.client("cloudformation"), stack, include_review=False)
    if not existing or not name:
        raise UpError(
            f"No Polyris platform stack found in {_region}.",
            f"Install it with {_c('polyris-up', CYAN)}, or pass --region / --stack.",
        )
    outputs = {o["OutputKey"]: o["OutputValue"] for o in existing.get("Outputs") or []}
    if OUTPUT_TEST_SFN_ARN not in outputs:
        raise UpError(f"Stack {name} has no {OUTPUT_TEST_SFN_ARN} output — is it a Polyris platform stack?")
    return name, outputs[OUTPUT_TEST_SFN_ARN]


# ── Release archive ──────────────────────────────────────────────────────────


def cache_dir(name: str) -> Path:
    """``~/.cache/polyris/<name>`` (respects XDG_CACHE_HOME); <name> is a version or "archives"."""
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "polyris" / name


def _download(url: str, dest: Path) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=60) as resp, open(tmp, "wb") as out:  # noqa: S310 — fixed https URL
        while chunk := resp.read(1 << 20):
            out.write(chunk)
    tmp.replace(dest)


def fetch_release(version: str) -> Path:
    """Download (once) and verify the platform archive for ``version``; return its path."""
    directory = cache_dir(version)
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / release_asset_name(version)
    if archive.is_file():
        return archive
    url = release_asset_url(version)
    print(f"   downloading {url}")
    try:
        _download(url + ".sha256", directory / f"{archive.name}.sha256")
        _download(url, archive)
    except OSError as e:  # urllib's HTTPError / URLError are OSError subclasses
        archive.unlink(missing_ok=True)
        raise UpError(
            f"Could not download the v{version} platform release: {e}",
            "Releases are published by CI on each tag; a version installed from an untagged "
            "commit has none. Install a released version, or pass --archive <file> built with "
            "scripts/build_platform_release.py.",
        )
    sha_words = (directory / f"{archive.name}.sha256").read_text().split()
    expected = sha_words[0] if sha_words else ""
    actual = hashlib.sha256(archive.read_bytes()).hexdigest()
    if actual != expected:
        archive.unlink()
        raise UpError(f"Checksum mismatch for {archive.name} (expected {expected}, got {actual}).",
                      "Run polyris-up again to re-download.")
    return archive


def extract_archive(archive: Path) -> Tuple[Path, Manifest]:
    """Extract into the cache (once) and return (root, manifest).

    Extracts into a temporary directory and renames it into place, so an
    interrupted extraction never leaves a half-filled tree that a later run
    would trust.
    """
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()[:16]
    root = cache_dir("archives") / f"extracted-{digest}"
    if not root.is_dir():
        root.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix=f".extracting-{digest}-", dir=root.parent))
        try:
            with zipfile.ZipFile(archive) as zf:
                for name in zf.namelist():
                    target = (tmp / name).resolve()
                    if not str(target).startswith(str(tmp.resolve()) + os.sep):
                        raise UpError(f"Refusing to extract {name!r}: path escapes the archive root.")
                zf.extractall(tmp)
            tmp.rename(root)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
    try:
        manifest = Manifest.from_dict(json.loads((root / MANIFEST_NAME).read_text()))
    except (OSError, ValueError) as e:
        raise UpError(f"Invalid release archive: {e}")
    return root, manifest


# ── S3 ───────────────────────────────────────────────────────────────────────
#
# Every request names the expected owner (ExpectedBucketOwner = this account).
# Bucket names are global and the artifacts bucket name is predictable, so
# without it another account could pre-create `polyris-artifacts-<you>-<region>`
# with a public policy, pre-plant "artifacts" at the content-addressed keys,
# and have CloudFormation deploy its code into your account. S3 itself rejects
# any request whose bucket belongs to someone else (403).


def bucket_owner_status(s3, bucket: str, *, owner: str) -> str:
    """'missing', 'ours' (owned by ``owner``) or 'taken' (exists, owned by another account)."""
    try:
        s3.head_bucket(Bucket=bucket, ExpectedBucketOwner=owner)
        return "ours"
    except ClientError as e:
        code = str(e.response.get("Error", {}).get("Code", ""))
        if code in ("404", "NoSuchBucket", "NotFound"):
            return "missing"
        if code in ("403", "AccessDenied", "Forbidden"):
            return "taken"
        raise


def ensure_artifacts_bucket(s3, bucket: str, *, region: str, owner: str) -> None:
    """Create the private, encrypted artifacts bucket if it doesn't exist (idempotent)."""
    status = bucket_owner_status(s3, bucket, owner=owner)
    if status == "ours":
        return
    if status == "taken":
        raise UpError(
            f"The artifacts bucket name {bucket} is taken by another AWS account.",
            "polyris-up will not use a bucket it doesn't own. The name includes your account id, so this "
            "is deliberate; report it to AWS and your security team.",
        )
    kwargs: Dict[str, Any] = {"Bucket": bucket}
    if region != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
    s3.create_bucket(**kwargs)
    s3.put_public_access_block(Bucket=bucket, ExpectedBucketOwner=owner, PublicAccessBlockConfiguration={
        "BlockPublicAcls": True, "IgnorePublicAcls": True,
        "BlockPublicPolicy": True, "RestrictPublicBuckets": True,
    })
    s3.put_bucket_encryption(Bucket=bucket, ExpectedBucketOwner=owner, ServerSideEncryptionConfiguration={
        "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}],
    })
    s3.put_bucket_tagging(Bucket=bucket, ExpectedBucketOwner=owner,
                          Tagging={"TagSet": [{"Key": PLATFORM_TAG_KEY, "Value": "artifacts"}]})
    print(f"   created {_c(bucket, CYAN)}")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _object_exists(s3, bucket: str, key: str, *, owner: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key, ExpectedBucketOwner=owner)
        return True
    except ClientError as e:
        if str(e.response.get("Error", {}).get("Code", "")) in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def _upload(s3, bucket: str, key: str, path: Path, *, owner: str) -> None:
    s3.upload_file(str(path), bucket, key, ExtraArgs={"ExpectedBucketOwner": owner})


def _upload_once(s3, bucket: str, key: str, path: Path, *, owner: str) -> bool:
    """Upload unless the key already exists. Keys carry a content digest, so existing = identical."""
    if _object_exists(s3, bucket, key, owner=owner):
        return False
    _upload(s3, bucket, key, path, owner=owner)
    return True


def tree_digest(root: Path) -> str:
    """Digest of a directory's relative paths and contents."""
    h = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        h.update(path.relative_to(root).as_posix().encode() + b"\0")
        h.update(hashlib.sha256(path.read_bytes()).digest())
    return h.hexdigest()


def upload_artifacts(
    s3, bucket: str, root: Path, manifest: Manifest, *, owner: str,
) -> Tuple[Dict[str, str], Dict[str, str], str]:
    """Upload code, definitions and the UI. Returns (function keys, state machine keys, UI prefix)."""
    uploaded = total = 0
    function_keys: Dict[str, str] = {}
    for logical_id, rel in manifest.functions.items():
        path = root / rel
        key = artifact_key(manifest.version, "lambdas", _sha256_file(path), path.name)
        uploaded += _upload_once(s3, bucket, key, path, owner=owner)
        total += 1
        function_keys[logical_id] = key
    state_machine_keys: Dict[str, str] = {}
    for logical_id, rel in manifest.state_machines.items():
        path = root / rel
        key = artifact_key(manifest.version, "sfn", _sha256_file(path), path.name)
        uploaded += _upload_once(s3, bucket, key, path, owner=owner)
        total += 1
        state_machine_keys[logical_id] = key
    ui_root = root / manifest.ui
    prefix = ui_prefix(manifest.version, tree_digest(ui_root))
    # The ROOT index.html goes last: its presence marks a complete upload for
    # the check below. (Route pages are index.html files too — pipelines/index.html
    # and so on — and must be uploaded before it.)
    files = sorted((p for p in ui_root.rglob("*") if p.is_file()),
                   key=lambda p: p.relative_to(ui_root).as_posix() == "index.html")
    total += len(files)
    if not _object_exists(s3, bucket, f"{prefix}/index.html", owner=owner):
        for path in files:
            _upload(s3, bucket, f"{prefix}/{path.relative_to(ui_root).as_posix()}", path, owner=owner)
        uploaded += len(files)
    reused = total - uploaded
    print(f"   artifacts: {uploaded} uploaded" + (f", {reused} already in the bucket" if reused else ""))
    return function_keys, state_machine_keys, prefix


def _upload_template(s3, *, bucket: str, region: str, version: str, template: Dict[str, Any], owner: str) -> str:
    body = json.dumps(template, sort_keys=True).encode()
    key = artifact_key(version, "template", hashlib.sha256(body).hexdigest(), "template.json")
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json", ExpectedBucketOwner=owner)
    return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"


def count_objects(s3, bucket: str, *, owner: str) -> int:
    """Objects (including old versions and delete markers) in a bucket; 0 if it's gone."""
    total = 0
    try:
        for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket, ExpectedBucketOwner=owner):
            total += len(page.get("Versions", [])) + len(page.get("DeleteMarkers", []))
    except ClientError as e:
        if "NoSuchBucket" in str(e):
            return 0
        raise
    return total


def empty_bucket(s3, bucket: str, *, owner: str) -> int:
    """Delete every object version and delete marker. Returns the number deleted."""
    deleted = 0
    try:
        for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket, ExpectedBucketOwner=owner):
            objects = [{"Key": v["Key"], "VersionId": v["VersionId"]}
                       for v in page.get("Versions", []) + page.get("DeleteMarkers", [])]
            for i in range(0, len(objects), 1000):
                s3.delete_objects(Bucket=bucket, ExpectedBucketOwner=owner,
                                  Delete={"Objects": objects[i:i + 1000], "Quiet": True})
            deleted += len(objects)
    except ClientError as e:
        if "NoSuchBucket" in str(e):
            return 0
        raise
    return deleted


# ── CloudFormation ───────────────────────────────────────────────────────────


def _summarize_change_set(change_set: Dict[str, Any]) -> None:
    counts: Dict[str, int] = {}
    for change in change_set.get("Changes", []):
        action = change.get("ResourceChange", {}).get("Action", "?")
        counts[action] = counts.get(action, 0) + 1
    print("   changes:  " + ", ".join(f"{n} {a.lower()}" for a, n in sorted(counts.items())))


# ── polyris-up ───────────────────────────────────────────────────────────────


def _template_params(template: Dict[str, Any]) -> List[str]:
    return list((template.get("Parameters") or {}).keys())


def run_up(args: argparse.Namespace) -> None:
    _header("Polyris platform")
    session, region, account = _session(args.profile, args.region)
    _print_target(profile=args.profile, region=region, account=account)
    cfn = session.client("cloudformation")
    s3 = session.client("s3")

    try:  # validate flags before asking anything
        overrides = parse_param_overrides(args.param or [])
    except ValueError as e:
        raise UpError(str(e))
    args.namespace_from_flags = bool(args.namespace or args.stage)
    if args.stack:
        stack_name, existing = find_platform_stack(cfn, args.stack)
    else:
        stack_name, existing = (None, None)
        if not (args.namespace or args.stage):
            stack_name, existing = find_platform_stack(cfn, None)
            if not existing:
                _warn_dead_platform_stacks(cfn)
                print("\n   No platform stack found — new install.")
                args.namespace = _prompt("Namespace (unique prefix, e.g. your org)")
                args.stage = _prompt("Stage", "dev")
        if not existing:
            # --namespace / --stage (or the answers) name the target:
            # `polyris-up --stage prod` next to an existing acme-dev must
            # create acme-prod, not update acme-dev.
            args.namespace = args.namespace or _prompt("Namespace (unique prefix, e.g. your org)")
            args.stage = args.stage or "dev"
            stack_name, existing = find_platform_stack(cfn, default_stack_name(args.namespace, args.stage))

    status = (existing or {}).get("StackStatus", "")
    if status == "REVIEW_IN_PROGRESS":
        # An install that stopped before its change set ran: the stack exists
        # but has no resources and no parameters. Finish it as a new install —
        # but only if it is ours (tagged, or holding a polyris change set), never
        # another tool's pending stack that happens to be named here.
        assert stack_name and existing is not None
        if not (is_platform_stack(existing) or _has_polyris_change_set(cfn, stack_name)):
            raise UpError(f"Stack {stack_name} is not a Polyris platform stack — refusing to change it.",
                          "Pick another --namespace / --stage, or name the right stack with --stack.")
        print(f"   {stack_name} is left from an interrupted install — finishing it as a new install.")
        existing = None
    elif status in DEAD_STACK_STATUSES:
        raise UpError(
            f"Stack {stack_name} is {status} and can't be updated.",
            f"Remove it with {_c(f'polyris-down --stack {stack_name}', CYAN)}, fix the cause shown in its events, "
            "then run polyris-up again.",
        )

    deployed: Optional[str] = None
    if existing:
        deployed = version_from_description(existing.get("Description"))
        print(f"   stack:    {_c(stack_name or '', CYAN)} (update, {deployed or 'unknown version'} → {__version__})")
        if existing.get("StackStatus", "").endswith("_IN_PROGRESS"):
            raise UpError(f"Stack {stack_name} is busy ({existing['StackStatus']}). Try again when it finishes.")
        if not is_platform_shaped(existing):
            raise UpError(f"Stack {stack_name} is not a Polyris platform stack — refusing to change it.",
                          "Pick another --namespace / --stage, or name the right stack with --stack.")
        if not is_platform_stack(existing) and not args.yes:
            print(_c(f"   {stack_name} was not created by polyris-up; it will be adopted (tagged and "
                     "switched to release artifacts).", YELLOW))
            _confirm_yes("Continue?")
        values: Dict[str, str] = {}
    else:
        if args.stack or args.namespace_from_flags:
            print(f"\n   No platform stack named {stack_name} — new install.")
        namespace = args.namespace or _prompt("Namespace (unique prefix, e.g. your org)")
        stage = args.stage or _prompt("Stage", "dev")
        errors = validate_namespace_stage(namespace, stage)
        if errors:
            raise UpError(errors[0], *errors[1:])
        stack_name = stack_name or default_stack_name(namespace, stage)
        for bucket in platform_bucket_names(namespace, stage):
            owner = bucket_owner_status(s3, bucket, owner=account)
            if owner == "taken":
                raise UpError(f"Bucket {bucket} already exists in another AWS account.",
                              "Bucket names are global — choose a different --namespace.")
            if owner == "ours":
                current = stack_using_namespace(cfn, namespace, stage)
                if current:
                    raise UpError(
                        f"Namespace {namespace!r} / stage {stage!r} is already installed in this account "
                        f"as stack {current} (not created by polyris-up).",
                        f"To manage that stack with polyris-up, run: polyris-up --stack {current}",
                    )
                raise UpError(f"Bucket {bucket} already exists in this account (left over from an earlier install?).",
                              "Delete it, or choose a different --namespace.")
        values = {PARAM_NAMESPACE: namespace, PARAM_STAGE: stage}
        if overrides.get(PARAM_ENABLE_COGNITO, "true") == "true":
            email = args.admin_email if args.admin_email is not None else _prompt(
                "Admin email (Cognito emails a temporary password; empty = create users later)",
                required=False)
            if email:
                values[PARAM_ADMIN_EMAIL] = email
        print(f"   stack:    {_c(stack_name, CYAN)} (create, v{__version__})")
    if args.admin_email is not None and existing:
        values[PARAM_ADMIN_EMAIL] = args.admin_email

    _header("Release artifacts")
    if args.archive:
        archive = Path(args.archive)
        if not archive.is_file():
            raise UpError(f"Archive not found: {archive}")
    else:
        archive = fetch_release(__version__)
    root, manifest = extract_archive(archive)
    if manifest.version != __version__ and not args.archive:
        raise UpError(f"Release archive is v{manifest.version}, installed polyris is v{__version__}.")
    if is_downgrade(deployed, manifest.version) and not args.allow_downgrade:
        raise UpError(
            f"{stack_name} runs v{deployed}; this would roll it back to v{manifest.version}.",
            "Install the newer polyris (re-run install.sh) — or pass --allow-downgrade if the rollback is intended.",
        )
    bucket = artifacts_bucket_name(account, region)
    ensure_artifacts_bucket(s3, bucket, region=region, owner=account)
    function_keys, sfn_keys, prefix = upload_artifacts(s3, bucket, root, manifest, owner=account)

    template = json.loads((root / manifest.template).read_text())
    template = rewrite_template(template, bucket=bucket, function_keys=function_keys,
                                state_machine_keys=sfn_keys, version=manifest.version)
    template_url = _upload_template(s3, bucket=bucket, region=region, version=manifest.version,
                                    template=template, owner=account)
    values.update({PARAM_ARTIFACTS_BUCKET: bucket, PARAM_UI_PREFIX: prefix})
    values.update(overrides)
    try:
        parameters = resolve_parameters(
            template_params=_template_params(template),
            existing=parameters_of(existing) if existing else None,
            values=values,
        )
    except ValueError as e:
        raise UpError(str(e))

    _header("CloudFormation")
    assert stack_name  # set on both branches above
    try:
        change_set = create_change_set(cfn, stack_name=stack_name, capabilities=CAPABILITIES,
                                       template_url=template_url, parameters=parameters,
                                       tags=platform_tags(existing))
    except ChangeSetError as e:
        raise UpError(str(e))
    if change_set is None:
        status = str((existing or {}).get("StackStatus", ""))
        if status.endswith("_FAILED"):
            # A failed update kept with --disable-rollback is not "up to date": nothing new to change
            # does not mean the resources that failed are fixed.
            raise UpError(f"Stack {stack_name} is {status} and this release changes nothing more.",
                          "Retry the failed update in the CloudFormation console (Retry, or Roll back), "
                          "then run polyris-up again.")
        print(_c(f"\n✅ {stack_name} is already up to date (v{manifest.version}).", BOLD_GREEN))
        _print_outputs(existing or {})
        return
    _summarize_change_set(change_set)
    risky = replaced_data_resources(change_set)
    if risky:
        print(_c("   This change would replace or remove resources that hold data:", RED))
        for r in risky:
            print(_c(f"     - {r}", RED))
        if not args.accept_data_loss:
            if not _is_interactive():
                raise UpError("Nothing was changed: this would delete data.",
                              "Re-run with --accept-data-loss if that is intended (--yes does not cover it).")
            _confirm_yes("Continue anyway?")
    # A first install that fails is removed with polyris-down, so only an update keeps what it changed.
    keep_on_failure = bool(args.disable_rollback) and existing is not None
    stop_narration = start_event_watcher(cfn, stack_name)
    try:
        execute_change_set(cfn, stack_name=stack_name, change_set=change_set, disable_rollback=keep_on_failure)
    except ChangeSetError as e:
        if keep_on_failure:
            raise UpError(str(e), "Nothing was rolled back (--disable-rollback): fix the cause, then run the "
                                  "same polyris-up command again.")
        raise UpError(str(e))
    finally:
        stop_narration()

    stack = describe_stack(cfn, stack_name) or {}
    print(_c(f"\n✅ Polyris v{manifest.version} is up: {stack_name}", BOLD_GREEN))
    _print_outputs(stack)
    if PARAM_ADMIN_EMAIL in values and values[PARAM_ADMIN_EMAIL] and existing is None:
        print(f"   Login:    {values[PARAM_ADMIN_EMAIL]} — Cognito has emailed a temporary password.")
    flags = next_step_flags(args, stack_name)
    print("\n   Next — a first pipeline that runs right away:")
    print(f"     {_c(f'polyris-init hello-world --hello{flags}', CYAN)}")
    print(f"     {_c(f'cd hello-world && polyris-deploy{flags}', CYAN)}   then press Run in the console")


def next_step_flags(args: argparse.Namespace, stack_name: str) -> str:
    """The target flags the user gave polyris-up, repeated for the next commands (leading space)."""
    parts = []
    if args.stack or args.namespace or args.stage:
        parts.append(f"--stack {stack_name}")
    if args.profile:
        parts.append(f"--profile {args.profile}")
    if args.region:
        parts.append(f"--region {args.region}")
    return "".join(f" {p}" for p in parts)


def _print_outputs(stack: Dict[str, Any]) -> None:
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs") or []}
    if OUTPUT_CONSOLE_URL in outputs:
        print(f"   Console:  {_c(outputs[OUTPUT_CONSOLE_URL], CYAN)}")


# ── polyris-down ─────────────────────────────────────────────────────────────


def pipelines_on_platform(session, cfn, stack: Dict[str, Any], stack_name: str) -> List[str]:
    """Pipelines still deployed on this platform.

    The platform's pipeline registry is the source of truth (ADR #39): every
    pipeline polyris-deploy registered there, whose state machine still exists,
    is live — whatever namespace its config.py gave it. Stacks named
    ``<Namespace>-<Stage>-polyris-*`` are added for pipelines that never
    registered. A row whose state machine can't be checked counts as live.
    """
    params = parameters_of(stack)
    prefix = f"{params.get(PARAM_NAMESPACE, '')}-{params.get(PARAM_STAGE, '')}-polyris-"
    # Pipeline names (dag ids), so a pipeline found both ways is listed once.
    found = {s["StackName"][len(prefix):] for s in all_stacks(cfn)
             if s["StackName"].startswith(prefix) and s["StackName"] != stack_name}
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs") or []}
    table = outputs.get("PipelineRegistryTable")
    if not table:
        return sorted(found)
    sfn = session.client("stepfunctions")
    try:
        pages = session.client("dynamodb").get_paginator("scan").paginate(
            TableName=table, ProjectionExpression="pipeline_name, sfn_arn")
        rows = [item for page in pages for item in page.get("Items", [])]
    except ClientError as e:
        if "ResourceNotFoundException" in str(e):
            return sorted(found)
        raise
    for row in rows:
        name = row.get("pipeline_name", {}).get("S", "")
        arn = row.get("sfn_arn", {}).get("S", "")
        if not name or name.startswith("_"):
            continue
        if arn:
            try:
                if sfn.describe_state_machine(stateMachineArn=arn).get("status") == "DELETING":
                    continue
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") == "StateMachineDoesNotExist":
                    continue  # destroyed pipeline: its registry row outlives it
        # No ARN yet (registration still running or failed) or an unknown
        # state: count it as live — a false "still deployed" is recoverable
        # with --yes, deleting the platform under a live pipeline is not.
        found.add(name)
    return sorted(found)



def run_down(args: argparse.Namespace) -> None:
    _header("Remove Polyris platform")
    session, region, account = _session(args.profile, args.region)
    _print_target(profile=args.profile, region=region, account=account)
    cfn = session.client("cloudformation")
    s3 = session.client("s3")

    stack_name, stack = find_platform_stack(cfn, args.stack, include_dead=True)
    if not stack or not stack_name:
        print(f"\n   No platform stack{f' named {stack_name}' if stack_name else ''} in {region} — nothing to remove.")
        return
    ours_pending = stack.get("StackStatus") == "REVIEW_IN_PROGRESS" and _has_polyris_change_set(cfn, stack_name)
    if not (is_platform_shaped(stack) or ours_pending):
        raise UpError(f"Stack {stack_name} is not a Polyris platform stack — refusing to delete it.")
    if stack.get("StackStatus", "").endswith("_IN_PROGRESS") and stack.get("StackStatus") != "REVIEW_IN_PROGRESS":
        raise UpError(f"Stack {stack_name} is busy ({stack['StackStatus']}). Try again when it finishes.")

    resources = cfn.get_paginator("list_stack_resources").paginate(StackName=stack_name)
    tables, buckets = [], {}
    for page in resources:
        for r in page["StackResourceSummaries"]:
            physical = r.get("PhysicalResourceId")  # absent when the resource never got created
            if not physical:
                continue
            if r["ResourceType"] == "AWS::DynamoDB::Table":
                tables.append(physical)
            elif r["ResourceType"] == "AWS::S3::Bucket":
                buckets[r["LogicalResourceId"]] = physical

    pipelines = pipelines_on_platform(session, cfn, stack, stack_name)
    if pipelines and not args.yes:
        raise UpError(
            f"{len(pipelines)} pipeline(s) still deployed on this platform: {', '.join(pipelines)}.",
            f"Remove them first with {_c('polyris-destroy', CYAN)} in each pipeline's directory "
            "(their schedules would keep firing), or pass --yes to delete the platform anyway.",
        )

    print(f"\n   Deleting {_c(stack_name, CYAN)} removes:")
    print(_c(f"     - {len(tables)} DynamoDB tables (run history, pipeline registry) — always deleted", YELLOW))
    counts = {logical: count_objects(s3, b, owner=account) for logical, b in buckets.items()}
    for logical, bucket in sorted(buckets.items()):
        print(f"     - bucket {bucket}: {counts[logical]} objects")
    results = buckets.get("ResultsBucket")
    if results and counts.get("ResultsBucket") and not args.delete_data:
        raise UpError(
            f"{results} holds {counts['ResultsBucket']} task results. Nothing was deleted.",
            "Back them up if you need them, then re-run with --delete-data.",
        )
    if not args.yes:
        _confirm_typed(stack_name, "Deleting the platform")

    for logical, bucket in buckets.items():
        if logical != "ResultsBucket" or args.delete_data:
            empty_bucket(s3, bucket, owner=account)
    stop_narration = start_event_watcher(cfn, stack_name)
    try:
        cfn.delete_stack(StackName=stack_name)
        try:
            wait_for_stack(cfn, stack_name=stack_name, waiter="stack_delete_complete")
        except WaiterError as e:
            raise UpError(f"Stack {stack_name} was not deleted: {e}")
    finally:
        stop_narration()
    print(_c(f"\n✅ {stack_name} deleted.", BOLD_GREEN))

    artifacts = artifacts_bucket_name(account, region)
    remaining = [s["StackName"] for s in all_stacks(cfn) if is_platform_stack(s)]
    if remaining:
        print(f"   Kept {artifacts}: still used by {', '.join(sorted(remaining))}.")
    elif bucket_owner_status(s3, artifacts, owner=account) == "ours":
        empty_bucket(s3, artifacts, owner=account)
        s3.delete_bucket(Bucket=artifacts, ExpectedBucketOwner=account)
        print(f"   Deleted {artifacts} (no platform stacks left in {region}).")


# ── Entry points ─────────────────────────────────────────────────────────────


def _common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile", help="AWS profile (default: the standard credential chain)")
    parser.add_argument("--region", help="AWS region (default: AWS_REGION or the profile's region)")
    parser.add_argument("--stack", help="Platform stack name (default: the one stack tagged as a Polyris platform)")
    parser.add_argument("--yes", action="store_true", help="Don't ask for confirmation (for CI)")


def _run(fn, args: argparse.Namespace) -> int:
    try:
        fn(args)
        return 0
    except UpError as e:
        print(_c(f"\n❌ {e}", RED), file=sys.stderr)
        for hint in e.hints:
            print(f"   {hint}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except (ClientError, BotoCoreError) as e:
        print(_c(f"\n❌ AWS error: {e}", RED), file=sys.stderr)
        return 1


def build_up_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="polyris-up",
        description="Install or upgrade the Polyris platform in your AWS account from the prebuilt "
                    f"release of the installed polyris (v{__version__}). Runs from any directory.",
        epilog="Examples:\n"
               "  polyris-up                                   # new install (asks 2-3 questions) or upgrade\n"
               "  polyris-up --namespace acme --admin-email me@acme.com --yes\n"
               "  polyris-up --param EnablePitr=true           # change one setting, keep the rest",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _common_args(parser)
    parser.add_argument("--namespace", help="Resource name prefix for a new install (unique: bucket names are global)")
    parser.add_argument("--stage", help="Stage name for a new install (default: dev)")
    parser.add_argument("--admin-email", help="Email of the first console admin (Cognito emails a temporary password)")
    parser.add_argument("--param", action="append", metavar="KEY=VALUE",
                        help="Set a template parameter (repeatable). Unset parameters keep their current value.")
    parser.add_argument("--archive", help="Deploy a local release archive instead of downloading one (for testing builds)")
    parser.add_argument("--allow-downgrade", action="store_true",
                        help="Allow installing an older release than the stack runs (a rollback)")
    parser.add_argument("--accept-data-loss", action="store_true",
                        help="Allow a change that replaces or removes a table, bucket, user pool or admin user "
                             "(--yes alone never does)")
    parser.add_argument("--disable-rollback", action="store_true",
                        help="If the update fails, keep what it already changed instead of rolling back "
                             "(for an upgrade whose release notes ask for it; a first install always rolls back)")
    colorize_parser(parser)
    return parser


def build_down_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="polyris-down",
        description="Delete the Polyris platform stack. Shows what will be removed and asks you to type "
                    "the stack name. DynamoDB tables (run history) are always deleted.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _common_args(parser)
    parser.add_argument("--delete-data", action="store_true",
                        help="Also delete task results in ResultsBucket (refused without this flag)")
    colorize_parser(parser)
    return parser


def main_up(argv: Optional[Iterable[str]] = None) -> int:
    return _run(run_up, build_up_parser().parse_args(list(argv) if argv is not None else None))


def main_down(argv: Optional[Iterable[str]] = None) -> int:
    return _run(run_down, build_down_parser().parse_args(list(argv) if argv is not None else None))

