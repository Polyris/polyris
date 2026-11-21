"""A stand-in for 1.3's run_task, until TR-5 rewrites run_task (ADR #137 §1).

It runs as the stack's run_task state machine (``RunTaskHelperSfn``), which the
dispatcher starts by name as the template's ``RUN_TASK_ARN`` names it. It keeps
run_task's contract with the rows and nothing more: its first state
moves the row from ``deps_ready`` to ``running`` under the generation it was
started with, and a raised generation ends it quietly (Rule 47). Then it waits
for the user's work, an external job that the test ends with ``finish_job``,
and writes the outcome under the same generation: ``success``, or
``waiting_decision`` with the decision deadline in ``attention-index``. It
writes no output and no facts: the dispatcher's core reads neither.
"""

from datetime import datetime, timedelta
from typing import Any, Callable, Dict

from polyris.coordination import attention
from polyris.coordination.times import format_utc

CLAIM = "run-task-stub-claim"
FINISH = "run-task-stub-finish"
#: The global decision timeout's default (``__global_settings__``, ADR #114).
DECISION_TIMEOUT = timedelta(hours=5)

DEFINITION: Dict[str, Any] = {
    "QueryLanguage": "JSONata",
    "StartAt": "Claim",
    "States": {
        "Claim": {"Type": "Task", "Resource": "arn:aws:states:::lambda:invoke",
                  "Arguments": {"FunctionName": CLAIM, "Payload": "{% $states.input %}"},
                  "Output": "{% $merge([$states.input, {'claimed': $states.result.Payload.claimed}]) %}",
                  "Next": "Claimed"},
        "Claimed": {"Type": "Choice", "Choices": [{"Condition": "{% $states.input.claimed %}", "Next": "Work"}],
                    "Default": "Fenced"},
        "Work": {"Type": "Task", "Resource": "arn:aws:states:::glue:startJobRun.sync",
                 "Arguments": {"JobName": "{% $states.input.task_name %}"},
                 "Output": "{% $merge([$states.input, {'outcome': 'success'}]) %}",
                 "Catch": [{"ErrorEquals": ["States.ALL"],
                            "Output": "{% $merge([$states.input, {'outcome': 'failed'}]) %}", "Next": "Finish"}],
                 "Next": "Finish"},
        "Finish": {"Type": "Task", "Resource": "arn:aws:states:::lambda:invoke",
                   "Arguments": {"FunctionName": FINISH, "Payload": "{% $states.input %}"}, "End": True},
        "Fenced": {"Type": "Succeed"},
    },
}


def handlers(client: Any, table: str, clock: Callable[[], datetime]) -> Dict[str, Callable[[Any, Any], Any]]:
    """The stub's two functions over the stack's tasks table, on the stack's clock."""

    def write(event: Dict[str, Any], read: str, expression: str, values: Dict[str, Any]) -> bool:
        try:
            client.update_item(
                TableName=table, Key={"execution_name": {"S": event["execution_name"]}},
                UpdateExpression=expression, ConditionExpression="#s = :read AND #g = :g",
                ExpressionAttributeNames={"#s": "status", "#g": "generation",
                                          **({"#att": attention.KEY, "#due": attention.DUE}
                                             if "#att" in expression else {})},
                ExpressionAttributeValues={":read": {"S": read}, ":g": {"N": str(event["generation"])}, **values})
            return True
        except client.exceptions.ConditionalCheckFailedException:
            return False                                       # another owner moved the row (Rule 47)

    def claim(event: Dict[str, Any], _: Any) -> Dict[str, Any]:
        now = format_utc(clock())
        return {"claimed": write(event, "deps_ready", "SET #s = :running, status_since = :now",
                                 {":running": {"S": "running"}, ":now": {"S": now}})}

    def finish(event: Dict[str, Any], _: Any) -> Dict[str, Any]:
        moment = clock()
        now = format_utc(moment)
        if event["outcome"] == "success":
            done = write(event, "running", "SET #s = :status, status_since = :now, finished_at = :now REMOVE #att, #due",
                         {":status": {"S": "success"}, ":now": {"S": now}})
        else:
            entry = attention.attention(attention.DECISION, event["execution_name"])
            done = write(event, "running",
                         "SET #s = :status, status_since = :now, decision_since = :now, decision_timeout = :timeout, "
                         "#att = :entry, #due = :deadline",
                         {":status": {"S": "waiting_decision"}, ":now": {"S": now},
                          ":timeout": {"N": str(int(DECISION_TIMEOUT.total_seconds()))}, ":entry": {"S": entry},
                          ":deadline": {"S": format_utc(moment + DECISION_TIMEOUT)}})
        return {"written": done}

    return {CLAIM: claim, FINISH: finish}
