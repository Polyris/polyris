"""Consumer: Lambda that reads all three upstream via xcom.get() (1.0.0 API).

Deploy this as `polyris-xcom-report` Lambda function.
Runtime: python3.12+. Requires `polyris>=1.0.0` in the deployment zip.

IAM: `PolyrisTaskReadPolicy` attached to the Lambda execution role (grants
     dynamodb:GetItem on pipeline-tokens for the xcom.pull() fallback path
     that xcom.get() uses when the event inject is truncated or the dep is
     undeclared). This Lambda does NOT call xcom.push() so it does NOT need
     PolyrisTaskWritePolicy.

Task Detail Input tab after run:
    Three colored per-upstream cards (1.0.0 UI):
      * extract_dict         green success, expandable JSON payload
      * extract_primitive    green success (or yellow "no output recorded" if
                             extract_primitive raised — trigger_rule=all_done
                             still lets this handler run)
      * aggregate_glue       green if the Glue script called xcom.push(),
                             yellow "AWS API response, not application data"
                             banner if the Glue script omitted the push
"""
from polyris import xcom, XComMissingError, XComUpstreamFailedError


def handler(event, _context):
    # Required upstream — loud by default. XComMissingError if never recorded,
    # XComUpstreamFailedError if the upstream's status isn't "success".
    dict_output = xcom.get(event, "extract_dict")

    # Tolerant read — the primitive producer might have raised (see
    # extract_primitive.py). raise_on_failure=False returns None instead of
    # raising XComUpstreamFailedError, so trigger_rule="all_done" consumers
    # can gracefully handle partial upstream failures.
    #
    # Note: raise_on_missing=True (default) still applies — if the DDB row
    # doesn't exist at all (dep never ran), XComMissingError is still raised.
    primitive_output = xcom.get(
        event,
        "extract_primitive",
        raise_on_failure=False,
    )

    # Service task producer — xcom.get() reads the same event.upstream inject
    # regardless of whether the producer was Lambda or Glue. If the Glue job
    # called xcom.push(), the value here is the pushed dict. If not, it's
    # the wrapper-collected AWS response ({"JobRunId": "..."}) — the Console
    # banner already flagged this, but our downstream would still fail here.
    glue_output = xcom.get(event, "aggregate_glue")

    result = {
        "dict_rows": dict_output["rows"],
        "primitive_value": primitive_output,   # int, list, None, ..., or None if failed
        "glue_total": glue_output.get("total_rows"),   # .get() in case it's AWS metadata
    }

    # Educational: show the full XCom error taxonomy the caller could catch.
    # (Not raised here — declarative documentation for readers.)
    _catchable_errors = (XComMissingError, XComUpstreamFailedError)
    _ = _catchable_errors  # silence linter

    return result
