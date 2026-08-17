"""Local copy of AWS's ``cfnresponse`` helper — stdlib-only.

CloudFormation calls this Lambda as a custom resource and blocks until it
receives a signed PUT to ``event['ResponseURL']``. AWS's ``cfnresponse``
module is only injected by the Lambda runtime for **inline** functions
(``ZipFile`` in ``AWS::Lambda::Function``). When the function is deployed
via SAM ``CodeUri`` — as this one is — that module is not on the search
path, so ``import cfnresponse`` fails, the handler crashes, and the stack
sits in CREATE_IN_PROGRESS for an hour before CloudFormation gives up.

Bundling this file eliminates that whole failure class. Kept to standard
library imports (no ``urllib3``) so it works on any Python 3 runtime with
no packaging.

Reference:
  https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/cfn-lambda-function-code-cfnresponsemodule.html
"""
from __future__ import annotations

import json
import urllib.request

SUCCESS = "SUCCESS"
FAILED = "FAILED"


def send(event, context, response_status, response_data,
         physical_resource_id=None, no_echo=False, reason=None):
    """PUT the CloudFormation-formatted response to the pre-signed URL.

    Any exception here is swallowed and printed to CloudWatch — a raise
    would abort the handler *before* it had signaled back, which is the
    exact stuck-stack failure this module was written to prevent.
    """
    body = json.dumps({
        "Status": response_status,
        "Reason": reason or f"See CloudWatch: {context.log_stream_name}",
        "PhysicalResourceId": physical_resource_id or context.log_stream_name,
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "NoEcho": no_echo,
        "Data": response_data,
    }).encode("utf-8")

    req = urllib.request.Request(
        url=event["ResponseURL"],
        data=body,
        method="PUT",
        # CFN's pre-signed URL rejects the default Content-Type; the empty
        # string is what the reference implementation sends.
        headers={"content-type": "", "content-length": str(len(body))},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"cfnresponse: HTTP {resp.status}")
    except Exception as e:  # pragma: no cover -- network path, exercised in AWS
        print(f"cfnresponse: send failed — {e}")
