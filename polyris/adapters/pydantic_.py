"""pydantic.BaseModel  →  List[polyris.Column].

A common DX pattern: data team defines pydantic models for input
validation, then has to redefine the same fields when declaring an
polyris Asset. This adapter eliminates the redeclaration.

Public surface:
    pydantic_to_columns(Model) -> List[Column]

Used by `Asset.from_pydantic(Model)`.

Type mapping (pydantic / Python typing  →  polyris):

    int                               → bigint
    float                             → double
    str                               → string
    bytes                             → binary
    bool                              → boolean
    datetime.date                     → date
    datetime.time                     → time
    datetime.datetime                 → timestamp(tz_aware depending on tz)
    decimal.Decimal                   → decimal(38, 9) by default
    uuid.UUID                         → uuid
    Optional[T] / T | None            → T with nullable=True
    list[T] / List[T]                 → array(T)
    dict[K, V] / Dict[K, V]           → map(K, V)
    pydantic.BaseModel (nested)       → struct(...recursively...)
    Literal[...] / Enum               → string

Pydantic-specific niceties:
    - Field(description=...) becomes Column.description
    - Field(default=...) becomes Column.default for JSON-safe scalars
    - Required fields → nullable=False
    - Optional fields  → nullable=True

We default-map int → bigint (not integer/int32) because pydantic int is
unbounded in Python and bigint is the safe AWS Glue / Iceberg landing.
Users who want narrower types can declare with `Annotated[int, …]` and
the metadata path (out of scope for v1; document instead).
"""

from __future__ import annotations

import datetime as _dt
import decimal as _dec
import enum as _enum
import typing as _typing
import uuid as _uuid
from typing import Any, List, TYPE_CHECKING, Tuple, Union

from ..schema import (
    Column, Schema, PolyrisType,
    array, bigint, binary, boolean, date, decimal, double, map_, string,
    struct, time, timestamp, uuid,
)

if TYPE_CHECKING:
    pass


def _require_pydantic():
    """Import pydantic with a clear actionable error if missing."""
    try:
        import pydantic  # noqa: F401
        return pydantic
    except ImportError as e:  # pragma: no cover -- pydantic is an installed optional dependency in CI; the not-installed guard is for end-user environments
        raise ImportError(
            "pydantic is required for polyris.adapters.pydantic_. "
            "Install with:  pip install 'polyris[pydantic]'"
        ) from e


def pydantic_to_columns(model_cls: type) -> Schema:
    """Convert a pydantic BaseModel subclass into a list of Column instances.

    Only pydantic v2 is supported; v1 is end-of-life and lacks the
    `model_fields` API we rely on.
    """
    pydantic = _require_pydantic()
    if not (isinstance(model_cls, type) and issubclass(model_cls, pydantic.BaseModel)):
        raise TypeError(
            f"pydantic_to_columns expects a pydantic.BaseModel subclass, "
            f"got {model_cls!r}"
        )

    fields = getattr(model_cls, "model_fields", None)
    if fields is None:  # pragma: no cover -- every pydantic v2 BaseModel exposes model_fields; this guards a v1/degenerate class the type check above already largely excludes
        raise TypeError(
            f"{model_cls.__name__} has no model_fields attribute — "
            f"polyris requires pydantic v2 (>=2.0)."
        )

    out: List[Column] = []
    for name, field_info in fields.items():
        annotation = field_info.annotation
        polyris_type, is_optional = _annotation_to_polyris(annotation)

        # Required-ness: pydantic considers a field required if it has no
        # default and no default_factory. We treat everything else as nullable.
        is_required = field_info.is_required()
        nullable = is_optional or not is_required

        description = (field_info.description or "").strip()

        # Default value — only safe scalars are stored; complex objects (lists,
        # dicts, callables, sentinels) are dropped to keep the schema dict
        # JSON-safe end-to-end.
        default: Any = None
        raw_default = field_info.default
        # Use the explicit pydantic Undefined sentinel rather than the value
        # itself — None is a valid default but means "no default" in pydantic
        # only when paired with the sentinel.
        if raw_default is not pydantic.fields.PydanticUndefined:
            if isinstance(raw_default, (str, int, float, bool)):
                default = raw_default

        out.append(Column(
            name=name,
            type=polyris_type,
            description=description,
            nullable=nullable,
            default=default,
        ))
    return out


# =============================================================================
# Internals: walk Python type annotations
# =============================================================================

def _annotation_to_polyris(ann: Any) -> Tuple[PolyrisType, bool]:
    """Resolve a Python type annotation to (polyris_type, is_optional).

    The is_optional flag is propagated from `Optional[T]` / `T | None`
    through the caller so it can set Column.nullable correctly without
    losing the inner type.
    """
    pydantic = _require_pydantic()

    # Unwrap Optional / Union — only Union[T, None] is meaningful for nullability.
    origin = _typing.get_origin(ann)
    args = _typing.get_args(ann)

    if origin is Union:
        non_none = [a for a in args if a is not type(None)]
        is_optional = len(non_none) < len(args)
        if len(non_none) == 1:
            inner_type, _ = _annotation_to_polyris(non_none[0])
            return inner_type, is_optional
        # Multiple non-None members (Union[int, str, ...]) — polyris has no
        # union type; collapse to string as the safe textual fallback and
        # mark optional from the original union.
        return string(), is_optional

    if origin in (list, _typing.List):
        if args:
            inner_type, _ = _annotation_to_polyris(args[0])
            return array(inner_type), False
        return array(string()), False

    if origin in (dict, _typing.Dict):
        if len(args) == 2:
            k_type, _ = _annotation_to_polyris(args[0])
            v_type, _ = _annotation_to_polyris(args[1])
            return map_(k_type, v_type), False
        return map_(string(), string()), False

    # Literal[...] and Enum subclasses — both are strings at the catalog level.
    if origin is _typing.Literal:
        return string(), False

    if isinstance(ann, type) and issubclass(ann, _enum.Enum):
        return string(), False

    # Nested pydantic model → struct
    if isinstance(ann, type) and issubclass(ann, pydantic.BaseModel):
        nested_cols = pydantic_to_columns(ann)
        if not nested_cols:
            # Empty model — fall back to single-string struct so we don't crash
            # on degenerate (but legal) pydantic definitions.
            return string(), False
        fields = {c.name: c.type for c in nested_cols}
        return struct(fields), False

    # Plain leaf types
    if ann is int:
        return bigint(), False
    if ann is float:
        return double(), False
    if ann is str:
        return string(), False
    if ann is bool:
        return boolean(), False
    if ann is bytes:
        return binary(), False
    if ann is _dt.date:
        return date(), False
    if ann is _dt.time:
        return time(), False
    if ann is _dt.datetime:
        return timestamp(tz_aware=False), False
    if ann is _dec.Decimal:
        return decimal(38, 9), False
    if ann is _uuid.UUID:
        return uuid(), False
    if ann is type(None):
        return string(), True

    # Anything else — best-effort string fallback. We deliberately don't raise
    # because pydantic ecosystems contain a lot of custom annotated types
    # (Annotated[str, AfterValidator(...)] etc.) that all serialize as their
    # base type. A string default is harmless; users who care about a tighter
    # type should use the explicit Column form.
    return string(), False


__all__ = ["pydantic_to_columns"]
