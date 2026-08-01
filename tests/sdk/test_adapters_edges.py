"""Adapter edge tests — pydantic annotation conversions, pyarrow type errors.

Closes the reachable remainder of the optional-dependency adapters (CLAUDE.md
#12/#13). Both pydantic and pyarrow are installed in CI, so these exercise the
real conversion paths; only the import guards and the boto3 Glue fetch are
``# pragma: no cover``.
"""
from __future__ import annotations

import datetime as _dt
import typing

import pytest

from polyris.adapters import pydantic_ as P
from polyris.adapters import pyarrow_ as PA
from polyris.schema import StringType, ArrayType, MapType, StructType, PolyrisType


# ============================================================ #
# pydantic — _annotation_to_polyris branches
# ============================================================ #
class TestPydanticAnnotations:
    def test_union_collapses_to_string(self):
        ty, _opt = P._annotation_to_polyris(typing.Union[int, str])
        assert isinstance(ty, StringType)

    def test_bare_list_defaults_to_string_array(self):
        ty, _opt = P._annotation_to_polyris(typing.List)
        assert isinstance(ty, ArrayType)

    def test_bare_dict_defaults_to_string_map(self):
        ty, _opt = P._annotation_to_polyris(typing.Dict)
        assert isinstance(ty, MapType)

    def test_literal_is_string(self):
        ty, _opt = P._annotation_to_polyris(typing.Literal["a", "b"])
        assert isinstance(ty, StringType)

    def test_time_annotation(self):
        ty, _opt = P._annotation_to_polyris(_dt.time)
        assert ty.__class__.__name__ == "TimeType"

    def test_nonetype_is_optional_string(self):
        ty, optional = P._annotation_to_polyris(type(None))
        assert isinstance(ty, StringType) and optional is True

    def test_nested_model_becomes_struct(self):
        pydantic = pytest.importorskip("pydantic")

        class Inner(pydantic.BaseModel):
            x: int

        ty, _opt = P._annotation_to_polyris(Inner)
        assert isinstance(ty, StructType)

    def test_empty_nested_model_falls_back_to_string(self):
        pydantic = pytest.importorskip("pydantic")

        class Empty(pydantic.BaseModel):
            pass

        ty, _opt = P._annotation_to_polyris(Empty)
        assert isinstance(ty, StringType)

    def test_non_model_raises(self):
        with pytest.raises(TypeError):
            P.pydantic_to_columns(object)


# ============================================================ #
# pyarrow — unsupported type errors
# ============================================================ #
class TestPyarrowTypeErrors:
    def test_unsupported_pyarrow_type(self):
        pa = pytest.importorskip("pyarrow")
        with pytest.raises(TypeError):
            PA._pa_type_to_polyris(pa.duration("s"))

    def test_unsupported_polyris_type(self):
        class FutureType(PolyrisType):
            pass

        with pytest.raises(TypeError):
            PA._polyris_type_to_pa(FutureType())
