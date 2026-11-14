"""The tags on every AWS resource polyris creates (ADR #132).

Every resource that CloudFormation can tag carries the same keys, so a cost
report or an IAM condition can select one installation, one stage, the shared
platform or one pipeline. ``sam/template.yaml`` spells the platform's tags;
``polyris-deploy`` builds a pipeline's with :func:`pipeline_tags`.
``tests/sdk/test_resource_tags.py`` checks every resource of both templates.
"""

from __future__ import annotations

from typing import Dict, List

#: The installation's namespace: the platform stack's ``Namespace`` parameter.
NAMESPACE = "polyris:namespace"

#: The stage: the platform stack's ``Stage`` parameter.
STAGE = "polyris:stage"

#: The part of polyris a resource belongs to: ``PLATFORM_COMPONENT`` or ``PIPELINE_COMPONENT``.
COMPONENT = "polyris:component"

#: The pipeline a resource belongs to. Pipeline resources only.
PIPELINE = "polyris:pipeline"

#: ``COMPONENT`` of the shared platform stack's resources.
PLATFORM_COMPONENT = "platform"

#: ``COMPONENT`` of the resources ``polyris-deploy`` creates for a pipeline.
PIPELINE_COMPONENT = "pipeline"


def pipeline_tags(namespace: str, stage: str, pipeline: str) -> List[Dict[str, str]]:
    """The tags of a resource in a pipeline's stack, in CloudFormation's ``Tags`` list form."""
    values = {NAMESPACE: namespace, STAGE: stage, COMPONENT: PIPELINE_COMPONENT, PIPELINE: pipeline}
    return [{"Key": key, "Value": value} for key, value in values.items()]
