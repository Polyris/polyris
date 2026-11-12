"""
UI Bootstrap — CloudFormation custom resource (ADR #131).

Installs the prebuilt console UI for stacks deployed by ``polyris-up``.

Create / Update:
  1. copy every object under ``SourceBucket/SourcePrefix/`` into ``DestBucket``
     (the ConsoleUiBucket), with the same Cache-Control split ``ui/deploy.sh``
     uses: HTML and ``config.js`` are never cached, content-hashed assets are
     immutable;
  2. delete files left over from the previous build;
  3. write ``config.js`` from this stack's values (``config_js.py``);
  4. invalidate the CloudFront distribution.

Delete:
  Empties ``DestBucket`` only when the parent stack itself is being torn
  down. A Delete also fires when an *update* removes this resource (the stack
  switched to a from-source ``sam deploy``); then the bucket is left alone,
  because ``ui/deploy.sh`` now owns its contents.

The PhysicalResourceId is stable (derived from ``DestBucket``). If it changed
between invocations, CloudFormation would follow every Update with a Delete
of the "old" resource and wipe the UI it had just installed.
"""
from __future__ import annotations

import mimetypes

import boto3
import cfnresponse
from config_js import render_config_js

s3 = boto3.client("s3")
cfn = boto3.client("cloudformation")
cloudfront = boto3.client("cloudfront")

CONFIG_JS_KEY = "config.js"
NO_CACHE = "public, max-age=0, must-revalidate"
IMMUTABLE = "public, max-age=31536000, immutable"

# Parent-stack statuses where the whole stack is going away (same rule as
# bucket_cleanup/index.py).
_TEARDOWN_STATUSES = frozenset({"DELETE_IN_PROGRESS", "ROLLBACK_IN_PROGRESS"})

# Next.js static export emits a few extensions mimetypes doesn't know on every
# runtime. A wrong Content-Type breaks the browser (e.g. JS served as
# octet-stream is not executed), so pin the ones the build produces.
_CONTENT_TYPES = {
    ".js": "application/javascript",
    ".css": "text/css",
    ".html": "text/html",
    ".json": "application/json",
    ".txt": "text/plain",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".ico": "image/x-icon",
}


def physical_resource_id(dest_bucket: str) -> str:
    """Stable id for this resource — see the module docstring."""
    return f"ui-bootstrap:{dest_bucket}"


def content_type_for(key: str) -> str:
    """Content-Type for an object key, by extension."""
    dot = key.rfind(".")
    ext = key[dot:].lower() if dot != -1 else ""
    if ext in _CONTENT_TYPES:
        return _CONTENT_TYPES[ext]
    guessed, _ = mimetypes.guess_type(key)
    return guessed or "application/octet-stream"


def cache_control_for(key: str) -> str:
    """HTML and config.js must always be re-fetched; everything else is content-hashed."""
    if key.endswith(".html") or key == CONFIG_JS_KEY:
        return NO_CACHE
    return IMMUTABLE


def _list_keys(bucket: str, prefix: str = "") -> list[str]:
    keys: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        keys.extend(o["Key"] for o in page.get("Contents", []))
    return keys


def _delete_keys(bucket: str, keys: list[str]) -> None:
    # delete_objects takes at most 1000 keys per call.
    for i in range(0, len(keys), 1000):
        chunk = keys[i:i + 1000]
        s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in chunk]})


def install_ui(props: dict, request_id: str) -> int:
    """Copy the UI, prune stale files, write config.js, invalidate. Returns files copied."""
    dest_bucket = props["DestBucket"]
    source_bucket = props["SourceBucket"]
    source_prefix = props["SourcePrefix"].rstrip("/") + "/"

    source_keys = _list_keys(source_bucket, source_prefix)
    if not source_keys:
        # Refuse to continue: pruning below would delete the live UI and leave
        # an empty site. The usual cause is an artifacts upload that failed.
        raise RuntimeError(f"No UI files found at s3://{source_bucket}/{source_prefix}")

    installed: set[str] = set()
    for src_key in source_keys:
        dest_key = src_key[len(source_prefix):]
        if not dest_key or dest_key == CONFIG_JS_KEY:
            continue  # config.js is always rendered from the stack, never copied
        s3.copy_object(
            CopySource={"Bucket": source_bucket, "Key": src_key},
            Bucket=dest_bucket,
            Key=dest_key,
            MetadataDirective="REPLACE",  # without it CacheControl/ContentType are ignored
            CacheControl=cache_control_for(dest_key),
            ContentType=content_type_for(dest_key),
        )
        installed.add(dest_key)

    stale = [k for k in _list_keys(dest_bucket) if k not in installed and k != CONFIG_JS_KEY]
    if stale:
        _delete_keys(dest_bucket, stale)

    s3.put_object(
        Bucket=dest_bucket,
        Key=CONFIG_JS_KEY,
        Body=render_config_js(
            api_url=props.get("ApiUrl", ""),
            user_pool_id=props.get("CognitoUserPoolId", ""),
            client_id=props.get("CognitoClientId", ""),
            region=props.get("Region", ""),
        ).encode("utf-8"),
        ContentType="application/javascript",
        CacheControl=NO_CACHE,
    )

    cloudfront.create_invalidation(
        DistributionId=props["DistributionId"],
        InvalidationBatch={"Paths": {"Quantity": 1, "Items": ["/*"]}, "CallerReference": request_id},
    )
    print(f"Installed {len(installed)} UI files, pruned {len(stale)}, wrote config.js → s3://{dest_bucket}/")
    return len(installed)


def stack_is_being_torn_down(stack_id: str) -> bool:
    """True only when the parent stack is being deleted. Any doubt → False (keep the files)."""
    if not stack_id:
        return False
    try:
        stacks = cfn.describe_stacks(StackName=stack_id).get("Stacks") or []
    except Exception as e:  # refuse to empty when we can't tell — a leftover bucket is recoverable
        print(f"describe_stacks failed ({e}) — leaving the UI bucket untouched")
        return False
    return bool(stacks) and stacks[0].get("StackStatus", "") in _TEARDOWN_STATUSES


def handler(event, context):
    props = event.get("ResourceProperties", {})
    request_type = event.get("RequestType", "")
    phys_id = physical_resource_id(props.get("DestBucket", ""))
    print(f"UiBootstrap event: {request_type}")

    if request_type == "Delete":
        # Always answer SUCCESS on Delete: FAILED would leave the stack stuck
        # in DELETE_FAILED. If emptying fails, the bucket's own deletion then
        # fails with a clear "bucket not empty" reason.
        try:
            if stack_is_being_torn_down(event.get("StackId", "")):
                keys = _list_keys(props["DestBucket"])
                _delete_keys(props["DestBucket"], keys)
                print(f"Emptied {len(keys)} objects from {props['DestBucket']}")
        except Exception as e:
            print(f"Error emptying {props.get('DestBucket')}: {e}")
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {}, physical_resource_id=phys_id)
        return

    try:
        copied = install_ui(props, event.get("RequestId", "ui-bootstrap"))
    except Exception as e:
        print(f"Error: {e}")
        cfnresponse.send(event, context, cfnresponse.FAILED, {}, physical_resource_id=phys_id, reason=str(e))
        return
    cfnresponse.send(event, context, cfnresponse.SUCCESS, {"FilesCopied": str(copied)},
                     physical_resource_id=phys_id)
