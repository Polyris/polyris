"""The platform template (``sam/template.yaml``) as a dict, for the tests that read its resources.

CloudFormation's short tags (``!Ref``, ``!Sub``, …) are no YAML that a plain loader reads. ``load_template``
keeps each one as ``{tag: value}`` (``tagged``, for a test that tells a reference from a string) or drops the
tag and keeps its value.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "sam" / "template.yaml"
TAGS = ("!Ref", "!Sub", "!GetAtt", "!Join", "!If", "!Not", "!Equals", "!And", "!Or",
        "!Condition", "!Select", "!Split", "!ImportValue", "!FindInMap")


def _loader(tagged: bool) -> type:
    class _Loader(yaml.SafeLoader):
        pass

    def _tag(loader: yaml.SafeLoader, node: yaml.Node) -> Any:
        if isinstance(node, yaml.ScalarNode):
            value: Any = loader.construct_scalar(node)
        elif isinstance(node, yaml.SequenceNode):
            value = loader.construct_sequence(node)
        else:
            return loader.construct_mapping(node)
        return {node.tag: value} if tagged else value

    for tag in TAGS:
        _Loader.add_constructor(tag, _tag)
    return _Loader


@lru_cache(maxsize=None)
def _load(tagged: bool) -> Any:
    return yaml.load(TEMPLATE_PATH.read_text(), Loader=_loader(tagged))


def load_template(*, tagged: bool) -> dict:
    """The template; read once per form, so a test must not change what it gets."""
    return _load(tagged)
