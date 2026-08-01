"""Schema edge tests — validation guards, unknown-type errors, JSON Schema export.

Closes the remaining branches in ``polyris.schema`` (CLAUDE.md #13): ``StructType``
field validation, the ``struct`` helper's type check, the ``Unknown PolyrisType``
guards in ``to_glue_string`` and the JSON Schema exporter, ``type_from_string``'s
decimal-arity check, and the per-type branches of the JSON Schema exporter.
"""
from __future__ import annotations

import pytest

from polyris import types as t
from polyris.schema import (
    StructType,
    struct,
    to_glue_string,
    type_from_string,
    _polyris_type_to_jsonschema as to_jsonschema,
)


class TestStructValidation:
    def test_empty_struct_raises(self):
        with pytest.raises(ValueError):
            StructType(fields=())

    def test_non_str_field_name_raises(self):
        with pytest.raises(ValueError):
            StructType(fields=((123, t.string()),))

    def test_struct_helper_rejects_non_type_value(self):
        with pytest.raises(TypeError):
            struct(bad="not a type")


class TestUnknownTypeGuards:
    def test_to_glue_string_unknown_raises(self):
        with pytest.raises(TypeError):
            to_glue_string(object())

    def test_jsonschema_unknown_raises(self):
        with pytest.raises(TypeError):
            to_jsonschema(object(), False)


class TestTypeFromStringErrors:
    def test_decimal_too_many_args(self):
        with pytest.raises(ValueError):
            type_from_string("decimal(1,2,3)")


class TestJsonSchemaExport:
    def test_float_is_number(self):
        assert to_jsonschema(t.float_(), False)["type"] == "number"

    def test_json_is_open(self):
        assert to_jsonschema(t.json_(), False) == {}

    def test_date_has_date_format(self):
        assert to_jsonschema(t.date(), False)["format"] == "date"

    def test_time_has_time_format(self):
        assert to_jsonschema(t.time(), False)["format"] == "time"

    def test_binary_is_base64(self):
        assert to_jsonschema(t.binary(), False)["contentEncoding"] == "base64"

    def test_fixed_binary_carries_length(self):
        out = to_jsonschema(t.fixed_binary(16), False)
        assert out["minLength"] == 16 and out["maxLength"] == 16
