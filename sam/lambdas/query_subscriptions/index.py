"""
Query subscriptions Lambda - finds tasks waiting on a completed dependency.

Used by notify_dependents helper because DynamoDB Query has no optimized 
Step Functions integration (only aws-sdk which doesn't support JSONata Arguments).

v0.79.3 (ADR #75) — DAL repository pattern. The boto3 dance + pagination
loop now lives in dal/__init__.py.
v0.79.4 (ADR #76) — structured JSON logging via shared `logger` module.
"""
from qs_dal import subscriptions_repo
from logger import log


def handler(event, context):
    """
    Query subscriptions table for tasks waiting on the completed task.

    Input: {task_name, pipeline_execution_short}
    Output: {subscribers: [{subscriber, wait_token, ...}]}
    """
    try:
        task_name = event.get('task_name')
        pipeline_execution_short = event.get('pipeline_execution_short')

        if not task_name or not pipeline_execution_short:
            log.error("handler", "Missing required fields",
                      task_name=task_name,
                      pipeline_execution_short=pipeline_execution_short)
            raise ValueError(
                f"Missing required fields: task_name={task_name}, "
                f"pipeline_execution_short={pipeline_execution_short}"
            )

        dependency_key = f"{task_name}-{pipeline_execution_short}"
        subscribers = subscriptions_repo.list_for_dependency(dependency_key)

        if len(subscribers) >= 10000:
            log.warn("handler", "Hit 10000 subscriber pagination cap",
                     dependency_key=dependency_key)

        log.info("handler", "Subscribers found",
                 dependency_key=dependency_key, count=len(subscribers))
        return {'subscribers': subscribers}

    except Exception as e:
        err_str = str(e)
        log.error("handler", "Query failed", error=err_str)
        # Re-raise permission errors so SFN Retry can catch them.
        # Silently returning [] would make notify_dependents skip real subscribers.
        if 'AccessDenied' in err_str or 'AccessDeniedException' in err_str:
            raise
        return {'subscribers': [], 'error': err_str}
