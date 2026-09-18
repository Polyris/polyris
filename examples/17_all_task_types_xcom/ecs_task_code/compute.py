"""ECS Fargate container code — reads resolve_athena via xcom.get, pushes metrics.

Bundle this into a Python container image that runs as the ``main`` container
of the ECS TaskDefinition ``polyris-test-xcom-all-compute``. Dockerfile
sketch::

    FROM python:3.12-slim
    RUN pip install polyris==1.0.0
    COPY compute.py /app/compute.py
    CMD ["python", "/app/compute.py"]

Runtime env vars the polyris wrapper injects into the container via
``ContainerOverrides.Environment`` (0.100.0+):
    POLYRIS_PIPELINE_NAME   — for xcom.get / xcom.push context resolution
    POLYRIS_RUN_DATE
    POLYRIS_TOKENS_TABLE
    POLYRIS_TASK_NAME       — set to this task's task_id
    POLYRIS_WRAPPER_RUN_ID  — wrapper execution ARN (for xcom.push idempotency)

IAM (on the ECS task role — NOT the execution role):
    - PolyrisTaskReadPolicy
    - PolyrisTaskWritePolicy

Task Detail Output tab after run (green card):
    {"metrics": {"score": 63.0}}
"""
from polyris import xcom


def main():
    # No `event` in a container — xcom.get(None, ...) reads DDB via POLYRIS_* env.
    verified = xcom.get(None, "resolve_athena")
    # verified = {"verified_count": N}

    score = verified["verified_count"] * 1.5
    xcom.push({"metrics": {"score": score, "source": "compute_ecs"}})


if __name__ == "__main__":
    main()
