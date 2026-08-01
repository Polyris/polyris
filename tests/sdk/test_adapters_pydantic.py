"""Tests for polyris.adapters.pydantic_ — pydantic model → List[Column]."""
from __future__ import annotations

import datetime as dt
import decimal
import enum
import uuid as uuid_mod
from typing import Optional

import pytest

pydantic = pytest.importorskip("pydantic")
from pydantic import BaseModel, Field

from polyris import schema as s
from polyris.adapters.pydantic_ import pydantic_to_columns


# =============================================================================
# Leaf type mapping
# =============================================================================

class TestLeafTypes:

    def test_int_maps_to_bigint(self):
        class M(BaseModel):
            x: int
        cols = pydantic_to_columns(M)
        assert cols[0].type == s.bigint()
        # Required field → not nullable.
        assert cols[0].nullable is False

    def test_float_maps_to_double(self):
        class M(BaseModel):
            x: float
        assert pydantic_to_columns(M)[0].type == s.double()

    def test_str_maps_to_string(self):
        class M(BaseModel):
            x: str
        assert pydantic_to_columns(M)[0].type == s.string()

    def test_bool_maps_to_boolean(self):
        class M(BaseModel):
            x: bool
        assert pydantic_to_columns(M)[0].type == s.boolean()

    def test_bytes_maps_to_binary(self):
        class M(BaseModel):
            x: bytes
        assert pydantic_to_columns(M)[0].type == s.binary()

    def test_date_maps_to_date(self):
        class M(BaseModel):
            x: dt.date
        assert pydantic_to_columns(M)[0].type == s.date()

    def test_datetime_maps_to_timestamp(self):
        class M(BaseModel):
            x: dt.datetime
        # Default to NTZ — pydantic doesn't infer tz-awareness from annotations.
        assert pydantic_to_columns(M)[0].type == s.timestamp(tz_aware=False)

    def test_decimal_maps_to_decimal_38_9(self):
        class M(BaseModel):
            x: decimal.Decimal
        # 38, 9 is the safe Glue/Iceberg landing for unbounded Decimal.
        assert pydantic_to_columns(M)[0].type == s.decimal(38, 9)

    def test_uuid_maps_to_uuid(self):
        class M(BaseModel):
            x: uuid_mod.UUID
        assert pydantic_to_columns(M)[0].type == s.uuid()


# =============================================================================
# Nullability and defaults
# =============================================================================

class TestNullabilityAndDefaults:

    def test_optional_marks_nullable(self):
        class M(BaseModel):
            x: Optional[int] = None
        col = pydantic_to_columns(M)[0]
        assert col.nullable is True
        assert col.type == s.bigint()

    def test_pep604_optional_syntax(self):
        # `int | None` form, equivalent to Optional[int].
        class M(BaseModel):
            x: int | None = None
        col = pydantic_to_columns(M)[0]
        assert col.nullable is True

    def test_required_field_not_nullable(self):
        class M(BaseModel):
            x: int
        assert pydantic_to_columns(M)[0].nullable is False

    def test_field_with_default_is_nullable(self):
        # Pydantic considers a field with a default as not required;
        # we surface that as nullable=True so downstream catalogs accept missing values.
        class M(BaseModel):
            x: int = 7
        col = pydantic_to_columns(M)[0]
        assert col.nullable is True
        assert col.default == 7

    def test_default_string_preserved(self):
        class M(BaseModel):
            x: str = "active"
        assert pydantic_to_columns(M)[0].default == "active"

    def test_default_complex_object_dropped(self):
        # Lists / dicts are not stored as defaults — only JSON-safe scalars.
        class M(BaseModel):
            tags: list[str] = []
        col = pydantic_to_columns(M)[0]
        assert col.default is None
        assert col.type == s.array(s.string())


# =============================================================================
# Field metadata: description, etc.
# =============================================================================

class TestFieldMetadata:

    def test_description_carried_over(self):
        class M(BaseModel):
            x: int = Field(description="Primary key")
        col = pydantic_to_columns(M)[0]
        assert col.description == "Primary key"

    def test_no_description_yields_empty_string(self):
        class M(BaseModel):
            x: int
        assert pydantic_to_columns(M)[0].description == ""


# =============================================================================
# Container types
# =============================================================================

class TestContainerTypes:

    def test_list_maps_to_array(self):
        class M(BaseModel):
            tags: list[str] = []
        col = pydantic_to_columns(M)[0]
        assert col.type == s.array(s.string())

    def test_dict_maps_to_map(self):
        class M(BaseModel):
            attrs: dict[str, int] = {}
        col = pydantic_to_columns(M)[0]
        assert col.type == s.map_(s.string(), s.bigint())

    def test_optional_list(self):
        class M(BaseModel):
            tags: Optional[list[str]] = None
        col = pydantic_to_columns(M)[0]
        assert col.nullable is True
        assert col.type == s.array(s.string())


# =============================================================================
# Nested models, enums, literals
# =============================================================================

class TestNestedAndExotic:

    def test_nested_model_maps_to_struct(self):
        class Address(BaseModel):
            street: str
            zip: int

        class User(BaseModel):
            name: str
            address: Address

        cols = pydantic_to_columns(User)
        names = [c.name for c in cols]
        assert names == ['name', 'address']
        assert cols[1].type == s.struct(street=s.string(), zip=s.bigint())

    def test_enum_maps_to_string(self):
        class Color(str, enum.Enum):
            RED = 'red'
            BLUE = 'blue'

        class M(BaseModel):
            color: Color

        cols = pydantic_to_columns(M)
        assert cols[0].type == s.string()


# =============================================================================
# Top-level adapter behaviour
# =============================================================================

class TestAdapterContract:

    def test_rejects_non_basemodel(self):
        with pytest.raises(TypeError, match="BaseModel"):
            pydantic_to_columns(dict)

    def test_rejects_non_class(self):
        with pytest.raises(TypeError, match="BaseModel"):
            pydantic_to_columns({"x": int})

    def test_field_order_preserved(self):
        class M(BaseModel):
            third: str
            first: int
            second: bool
        names = [c.name for c in pydantic_to_columns(M)]
        # Pydantic preserves declaration order; we must too for stable diffs.
        assert names == ['third', 'first', 'second']


# =============================================================================
# Asset.from_pydantic integration
# =============================================================================

class TestAssetFromPydantic:

    def test_basic_construction(self):
        from polyris import Asset

        class Order(BaseModel):
            order_id: int = Field(description="Primary key")
            amount: decimal.Decimal
            tags: list[str] = []

        a = Asset.from_pydantic(Order, name='retail/orders')
        assert a.name == 'retail/orders'
        assert len(a.schema) == 3
        assert a.schema[0].name == 'order_id'
        assert a.schema[0].description == 'Primary key'

    def test_default_name_from_model_class(self):
        from polyris import Asset

        class MyTable(BaseModel):
            id: int

        a = Asset.from_pydantic(MyTable)
        assert a.name == 'MyTable'

    def test_rejects_explicit_schema_kwarg(self):
        from polyris import Asset

        class M(BaseModel):
            x: int

        with pytest.raises(TypeError, match='from_pydantic'):
            Asset.from_pydantic(M, name='x', schema=[])
