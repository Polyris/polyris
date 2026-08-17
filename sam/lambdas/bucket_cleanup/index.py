"""
Bucket Cleanup — CloudFormation Custom Resource

Empties the S3 buckets listed in ``ResourceProperties.Buckets`` on stack
delete. CloudFormation cannot delete a non-empty bucket, so without this
step ``sam delete`` fails with:

    The following resource(s) failed to delete: [ResultsBucket, ConsoleUiBucket]

Enabled by the ``AutoEmptyBucketsOnDelete`` template parameter — see the
guard on the resources that reference this function in ``sam/template.yaml``.
Never enable in production without a separate backup strategy: the
ResultsBucket holds task xcom output and once emptied it is gone.

IMPORTANT — the Delete callback fires in two very different situations:

 1. The user ran ``sam delete`` and the whole stack is going away. The
    buckets ARE about to be deleted; empty them.
 2. The user changed ``AutoEmptyBucketsOnDelete`` from ``true`` to ``false``
    and ran ``sam deploy`` (an *update*, not a delete). CFN removes this
    custom resource as part of the update. The buckets are NOT going
    away — emptying them here would silently wipe task results the user
    still cares about.

We disambiguate by reading the parent stack's ``StackStatus``. Only when
the stack itself is being torn down (``*DELETE_IN_PROGRESS`` /
``ROLLBACK_IN_PROGRESS`` on a failed initial CREATE) do we actually
empty. Everything else is treated as an update: signal SUCCESS and leave
the buckets alone.
"""
import boto3
import cfnresponse

s3 = boto3.client("s3")
cfn = boto3.client("cloudformation")

# Parent-stack statuses where the whole stack is being removed. Anything
# else on Delete means CFN is just deleting THIS custom resource as part
# of a broader update — the buckets are not going anywhere and must not
# be emptied.
_TEARDOWN_STATUSES = frozenset({
    "DELETE_IN_PROGRESS",
    # ROLLBACK_IN_PROGRESS: a fresh CREATE failed, CFN is undoing everything.
    # The whole stack ends up gone, so treating this like a delete is safe.
    "ROLLBACK_IN_PROGRESS",
})


def _empty_bucket(bucket: str) -> int:
    """Delete every object and version from *bucket*. Returns the count.

    ``list_objects_v2`` misses old versions on a versioned bucket, so
    ``list_object_versions`` is always paged too. Missing bucket is not an
    error — the caller may be running against a stack whose bucket was
    already removed by a prior partial delete."""
    deleted = 0
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket):
            objects = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if objects:
                s3.delete_objects(Bucket=bucket, Delete={"Objects": objects})
                deleted += len(objects)

        paginator = s3.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=bucket):
            versions = [{"Key": v["Key"], "VersionId": v["VersionId"]}
                        for v in page.get("Versions", [])]
            markers = [{"Key": m["Key"], "VersionId": m["VersionId"]}
                       for m in page.get("DeleteMarkers", [])]
            objects = versions + markers
            if objects:
                s3.delete_objects(Bucket=bucket, Delete={"Objects": objects})
                deleted += len(objects)
    except s3.exceptions.NoSuchBucket:
        print(f"Bucket {bucket} already gone — nothing to empty")
    return deleted


def _stack_is_being_torn_down(stack_id: str) -> bool:
    """Return True only when the whole parent stack is going away.

    A Delete event on this custom resource fires both for a real
    ``sam delete`` and for a stack UPDATE that removes this resource (e.g.
    the user toggled ``AutoEmptyBucketsOnDelete`` from ``true`` to
    ``false``). Emptying the buckets in the update case is silent data
    loss, so we gate on the parent stack's own status.

    On any AWS error we default to ``False`` — refuse to empty. Skipping
    when we shouldn't just means ``sam delete`` will error on non-empty
    buckets; emptying when we shouldn't destroys real data.
    """
    if not stack_id:
        return False
    try:
        stacks = cfn.describe_stacks(StackName=stack_id).get("Stacks") or []
        if not stacks:
            return False
        status = stacks[0].get("StackStatus", "")
        return status in _TEARDOWN_STATUSES
    except Exception as e:
        print(f"describe_stacks failed ({e}) — refusing to empty for safety")
        return False


def handler(event, context):
    request_type = event.get("RequestType", "")
    print(f"BucketCleanup event: {request_type}")

    # Create / Update — nothing to do. This resource exists only to receive
    # the Delete callback; touching buckets on Create/Update would risk
    # racing a real deploy.
    if request_type != "Delete":
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {})
        return

    # Guard against the "toggle-off during sam deploy" data-loss trap
    # described in the module docstring. If the parent stack isn't actually
    # being deleted, treat this like an update and leave the buckets alone.
    if not _stack_is_being_torn_down(event.get("StackId", "")):
        print("Parent stack is not being torn down — skipping bucket empty "
              "(likely an update that removed this custom resource).")
        cfnresponse.send(event, context, cfnresponse.SUCCESS,
                          {"Skipped": "parent-stack-not-deleting"})
        return

    try:
        buckets = event.get("ResourceProperties", {}).get("Buckets", []) or []
        for bucket in buckets:
            if not bucket:
                continue
            count = _empty_bucket(bucket)
            print(f"Emptied {bucket} — deleted {count} objects/versions")
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {})
    except Exception as e:
        # Always SUCCESS on Delete — a FAILED here leaves the stack stuck in
        # DELETE_FAILED forever. The user can still manually remove any
        # leftover objects; failing loud in CloudWatch is better than
        # blocking the stack.
        print(f"BucketCleanup error (returning SUCCESS to unblock stack): {e}")
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {"Warning": str(e)})
