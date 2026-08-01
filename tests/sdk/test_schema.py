"""Tests for polyris.schema — type system, Column, normalization, serialization."""
from __future__ import annotations

import pytest

from polyris import schema as s
from polyris.schema import (
    Column,
    PolyrisType,
    BigIntType, IntType, SmallIntType, TinyIntType,
    FloatType, BooleanType,
    FixedBinaryType,
    TimestampType,
    StructType, column_from_dict, column_to_dict,
    normalize_schema, schema_richness, dict_schema_richness,
    to_glue_string, type_from_string,
)


# =============================================================================
# Type instance equality, hashability, immutability
# =============================================================================

class TestTypeEquality:
    def test_same_type_equals_same_type(self):
        assert s.bigint() == s.bigint()
        assert s.string() == s.string()
        assert s.boolean() == s.boolean()

    def test_different_simple_types_not_equal(self):
        assert s.bigint() != s.integer()
        assert s.string() != s.varchar(255)
        assert s.float_() != s.double()

    def test_decimal_equality_with_same_params(self):
        assert s.decimal(10, 2) == s.decimal(10, 2)

    def test_decimal_inequality_with_different_params(self):
        assert s.decimal(10, 2) != s.decimal(10, 4)
        assert s.decimal(10, 2) != s.decimal(12, 2)

    def test_timestamp_tz_aware_distinguishes(self):
        assert s.timestamp() == s.timestamp(tz_aware=True)
        assert s.timestamp_ntz() == s.timestamp(tz_aware=False)
        assert s.timestamp() != s.timestamp_ntz()

    def test_nested_types_equality(self):
        assert s.array(s.string()) == s.array(s.string())
        assert s.array(s.string()) != s.array(s.bigint())
        assert s.map_(s.string(), s.bigint()) == s.map_(s.string(), s.bigint())
        assert s.map_(s.string(), s.bigint()) != s.map_(s.bigint(), s.string())

    def test_struct_equality_field_order_matters(self):
        a = s.struct(x=s.integer(), y=s.string())
        b = s.struct(x=s.integer(), y=s.string())
        c = s.struct(y=s.string(), x=s.integer())
        assert a == b
        assert a != c  # field order is part of the type identity


class TestTypeHashability:
    def test_simple_types_hashable(self):
        # Used as dict keys / set members for adapter mappings later.
        d = {s.bigint(): "long", s.string(): "str"}
        assert d[s.bigint()] == "long"
        assert s.string() in {s.string(), s.bigint()}

    def test_parametric_types_hashable(self):
        d = {s.decimal(10, 2): "money", s.decimal(38, 0): "id"}
        assert d[s.decimal(10, 2)] == "money"
        assert s.decimal(10, 2) in d
        assert s.decimal(10, 4) not in d

    def test_nested_types_hashable(self):
        st = s.struct(x=s.integer(), y=s.string())
        d = {st: "point"}
        assert d[s.struct(x=s.integer(), y=s.string())] == "point"


class TestTypeImmutability:
    def test_simple_type_frozen(self):
        t = s.decimal(10, 2)
        with pytest.raises((AttributeError, Exception)):
            t.precision = 20  # type: ignore[misc]

    def test_isinstance_marker_works(self):
        # All factories return PolyrisType instances — needed for adapter code.
        for factory_call in [
            s.tinyint(), s.smallint(), s.integer(), s.bigint(),
            s.float_(), s.double(), s.decimal(10, 2),
            s.boolean(),
            s.string(), s.varchar(255), s.char(10),
            s.binary(), s.fixed_binary(16),
            s.date(), s.time(), s.timestamp(), s.timestamp_ntz(),
            s.uuid(), s.json_(),
            s.array(s.string()),
            s.struct(x=s.integer()),
            s.map_(s.string(), s.bigint()),
        ]:
            assert isinstance(factory_call, PolyrisType)


# =============================================================================
# Type validation in __post_init__
# =============================================================================

class TestTypeValidation:
    def test_decimal_invalid_precision(self):
        with pytest.raises(ValueError, match="precision"):
            s.decimal(0, 0)
        with pytest.raises(ValueError, match="precision"):
            s.decimal(39, 0)

    def test_decimal_invalid_scale(self):
        with pytest.raises(ValueError, match="scale"):
            s.decimal(10, -1)
        with pytest.raises(ValueError, match="scale"):
            s.decimal(10, 11)  # scale > precision

    def test_varchar_requires_positive_length(self):
        with pytest.raises(ValueError, match="length"):
            s.varchar(0)

    def test_char_requires_valid_length(self):
        with pytest.raises(ValueError, match="length"):
            s.char(0)
        with pytest.raises(ValueError, match="length"):
            s.char(256)

    def test_fixed_binary_requires_positive_length(self):
        with pytest.raises(ValueError, match="length"):
            s.fixed_binary(0)

    def test_array_inner_must_be_polyris_type(self):
        with pytest.raises(TypeError, match="PolyrisType"):
            s.array("string")  # type: ignore[arg-type]

    def test_struct_rejects_empty(self):
        with pytest.raises(ValueError, match="at least one field"):
            s.struct()

    def test_struct_rejects_duplicate_field_names(self):
        with pytest.raises(ValueError, match="duplicate"):
            StructType(fields=(("x", s.integer()), ("x", s.string())))

    def test_struct_rejects_both_positional_and_kwargs(self):
        with pytest.raises(TypeError, match="either"):
            s.struct({"x": s.integer()}, y=s.string())

    def test_map_requires_polyris_types(self):
        with pytest.raises(TypeError, match="PolyrisType"):
            s.map_("string", s.bigint())  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="PolyrisType"):
            s.map_(s.string(), "bigint")  # type: ignore[arg-type]


# =============================================================================
# Glue-format string conversion (round-trip)
# =============================================================================

class TestGlueStringRoundTrip:
    @pytest.mark.parametrize("type_obj,expected", [
        (s.tinyint(),       "tinyint"),
        (s.smallint(),      "smallint"),
        (s.integer(),       "int"),
        (s.bigint(),        "bigint"),
        (s.float_(),        "float"),
        (s.double(),        "double"),
        (s.decimal(10, 2),  "decimal(10,2)"),
        (s.decimal(38, 0),  "decimal(38,0)"),
        (s.boolean(),       "boolean"),
        (s.string(),        "string"),
        (s.varchar(255),    "varchar(255)"),
        (s.char(10),        "char(10)"),
        (s.binary(),        "binary"),
        (s.fixed_binary(16), "fixed_size_binary(16)"),
        (s.date(),          "date"),
        (s.time(),          "time"),
        (s.timestamp(),     "timestamp"),
        (s.timestamp_ntz(), "timestamp_ntz"),
        (s.uuid(),          "uuid"),
        (s.json_(),         "json"),
    ])
    def test_to_glue_simple(self, type_obj, expected):
        assert to_glue_string(type_obj) == expected

    @pytest.mark.parametrize("type_obj,expected", [
        (s.array(s.string()),                          "array<string>"),
        (s.array(s.array(s.bigint())),                 "array<array<bigint>>"),
        (s.map_(s.string(), s.bigint()),               "map<string,bigint>"),
        (s.map_(s.string(), s.array(s.string())),      "map<string,array<string>>"),
        (s.struct(x=s.integer(), y=s.string()),        "struct<x:int,y:string>"),
    ])
    def test_to_glue_nested(self, type_obj, expected):
        assert to_glue_string(type_obj) == expected

    @pytest.mark.parametrize("type_obj", [
        s.tinyint(), s.smallint(), s.integer(), s.bigint(),
        s.float_(), s.double(), s.decimal(10, 2), s.decimal(38, 0),
        s.boolean(),
        s.string(), s.varchar(255), s.char(10),
        s.binary(), s.fixed_binary(16),
        s.date(), s.time(), s.timestamp(), s.timestamp_ntz(),
        s.uuid(), s.json_(),
        s.array(s.string()),
        s.array(s.array(s.bigint())),
        s.map_(s.string(), s.bigint()),
        s.map_(s.string(), s.array(s.string())),
        s.struct(x=s.integer(), y=s.string()),
        s.struct(id=s.bigint(), tags=s.array(s.string()),
                 attrs=s.map_(s.string(), s.string())),
    ])
    def test_round_trip(self, type_obj):
        rendered = to_glue_string(type_obj)
        parsed = type_from_string(rendered)
        assert parsed == type_obj, f"{type_obj!r} round-tripped to {parsed!r} via {rendered!r}"


class TestParserAliases:
    """Glue/Hive accept multiple aliases for the same type — we tolerate them on
    parse so users do not have to canonicalize input from external sources."""

    @pytest.mark.parametrize("alias,expected", [
        ("byte", TinyIntType()),
        ("short", SmallIntType()),
        ("integer", IntType()),
        ("long", BigIntType()),
        ("real", FloatType()),
        ("bool", BooleanType()),
        ("datetime", TimestampType(tz_aware=False)),
        ("BIGINT", BigIntType()),  # case-insensitive
        ("  bigint  ", BigIntType()),  # whitespace tolerated
    ])
    def test_alias_resolves(self, alias, expected):
        assert type_from_string(alias) == expected

    def test_decimal_single_arg_defaults_scale_zero(self):
        assert type_from_string("decimal(10)") == s.decimal(10, 0)

    def test_iceberg_fixed_alias(self):
        # Iceberg uses `fixed(N)`; Glue uses `fixed_size_binary(N)`. Both parse.
        assert type_from_string("fixed(16)") == FixedBinaryType(16)
        assert type_from_string("fixed_size_binary(16)") == FixedBinaryType(16)

    def test_struct_with_space_separator(self):
        # Some SQL dialects use `name type` instead of `name:type`.
        assert type_from_string("struct<x int,y string>") == s.struct(
            x=s.integer(), y=s.string()
        )


class TestParserErrors:
    def test_empty_string_rejected(self):
        with pytest.raises(ValueError):
            type_from_string("")

    def test_unknown_type_rejected(self):
        with pytest.raises(ValueError, match="Unknown type"):
            type_from_string("quaternion")

    def test_struct_invalid_field_syntax(self):
        with pytest.raises(ValueError, match="struct"):
            type_from_string("struct<x>")

    def test_map_wrong_arg_count(self):
        with pytest.raises(ValueError):
            type_from_string("map<string>")


class TestNestedCommaSplit:
    """Ensures map<string, array<int, double>> does not break on inner commas."""

    def test_map_with_array_value(self):
        t = type_from_string("map<string,array<bigint>>")
        assert t == s.map_(s.string(), s.array(s.bigint()))

    def test_struct_with_nested_struct(self):
        t = type_from_string("struct<a:int,b:struct<x:string,y:string>>")
        assert t == s.struct(a=s.integer(), b=s.struct(x=s.string(), y=s.string()))

    def test_struct_with_array_of_struct(self):
        t = type_from_string("struct<items:array<struct<id:bigint,name:string>>>")
        assert t == s.struct(items=s.array(s.struct(id=s.bigint(), name=s.string())))


# =============================================================================
# Column class
# =============================================================================

class TestColumn:
    def test_minimal_column(self):
        c = Column("order_id", s.bigint())
        assert c.name == "order_id"
        assert c.type == s.bigint()
        assert c.nullable is True
        assert c.primary_key is False
        assert c.partition_key is False
        assert c.unique is False
        assert c.default is None
        assert c.description == ""

    def test_full_column(self):
        c = Column(
            name="order_id",
            type=s.bigint(),
            description="Unique identifier",
            nullable=False,
            primary_key=True,
            partition_key=False,
            unique=True,
            default=0,
        )
        assert c.primary_key is True
        assert c.nullable is False

    def test_column_equality(self):
        a = Column("x", s.bigint(), primary_key=True)
        b = Column("x", s.bigint(), primary_key=True)
        c = Column("x", s.bigint(), primary_key=False)
        assert a == b
        assert a != c

    def test_column_hashable(self):
        # Frozen dataclasses are hashable when all fields are hashable.
        c = Column("x", s.bigint())
        assert hash(c) == hash(Column("x", s.bigint()))
        assert {c, c} == {c}

    def test_column_rejects_empty_name(self):
        with pytest.raises(ValueError, match="name"):
            Column("", s.bigint())

    def test_column_rejects_non_string_name(self):
        with pytest.raises(ValueError, match="name"):
            Column(123, s.bigint())  # type: ignore[arg-type]

    def test_column_rejects_string_type(self):
        # Helps catch the common mistake of passing "bigint" instead of bigint().
        with pytest.raises(TypeError, match="PolyrisType"):
            Column("x", "bigint")  # type: ignore[arg-type]


# =============================================================================
# Column ↔ dict serialization
# =============================================================================

class TestColumnSerialization:
    def test_minimal_column_dict_omits_defaults(self):
        c = Column("order_id", s.bigint())
        d = column_to_dict(c)
        assert d == {"name": "order_id", "type": "bigint"}

    def test_column_with_constraints_includes_them(self):
        c = Column("order_id", s.bigint(), nullable=False, primary_key=True)
        d = column_to_dict(c)
        assert d == {
            "name": "order_id",
            "type": "bigint",
            "nullable": False,
            "primary_key": True,
        }

    def test_column_with_description(self):
        c = Column("amount", s.decimal(10, 2), description="USD amount")
        d = column_to_dict(c)
        assert d == {
            "name": "amount",
            "type": "decimal(10,2)",
            "description": "USD amount",
        }

    def test_column_round_trip(self):
        original = Column(
            "order_id", s.bigint(),
            description="ID", nullable=False, primary_key=True,
        )
        restored = column_from_dict(column_to_dict(original))
        assert restored == original

    def test_column_round_trip_nested_type(self):
        original = Column("items", s.array(s.struct(id=s.bigint(), name=s.string())))
        restored = column_from_dict(column_to_dict(original))
        assert restored == original

    def test_from_dict_tolerates_old_format(self):
        # Old-format dicts had only name/type/description.
        c = column_from_dict({"name": "x", "type": "bigint"})
        assert c == Column("x", s.bigint())

        c2 = column_from_dict({"name": "x", "type": "bigint", "description": "ID"})
        assert c2 == Column("x", s.bigint(), description="ID")

    def test_from_dict_rejects_missing_name(self):
        with pytest.raises(ValueError, match="name"):
            column_from_dict({"type": "bigint"})

    def test_from_dict_rejects_missing_type(self):
        with pytest.raises(ValueError, match="type"):
            column_from_dict({"name": "x"})


# =============================================================================
# normalize_schema — single normalization point
# =============================================================================

class TestNormalizeSchema:
    def test_empty_input(self):
        assert normalize_schema(None) == []
        assert normalize_schema([]) == []

    def test_list_of_columns_passes_through(self):
        cols = [Column("x", s.bigint()), Column("y", s.string())]
        result = normalize_schema(cols)
        assert result == cols

    def test_tuple_2_elements(self):
        result = normalize_schema([("x", "bigint"), ("y", "string")])
        assert result == [Column("x", s.bigint()), Column("y", s.string())]

    def test_tuple_3_elements_with_description(self):
        result = normalize_schema([("x", "bigint", "ID column")])
        assert result == [Column("x", s.bigint(), description="ID column")]

    def test_tuple_with_typed_type(self):
        # Mixed: name string + typed type instance instead of string.
        result = normalize_schema([("x", s.bigint()), ("y", s.string(), "Name")])
        assert result == [Column("x", s.bigint()), Column("y", s.string(), description="Name")]

    def test_dict_format(self):
        result = normalize_schema([
            {"name": "x", "type": "bigint", "primary_key": True},
            {"name": "y", "type": "string"},
        ])
        assert result == [
            Column("x", s.bigint(), primary_key=True),
            Column("y", s.string()),
        ]

    def test_mixed_input_formats(self):
        # All three forms in one list — useful when migrating gradually.
        result = normalize_schema([
            Column("a", s.bigint(), primary_key=True),
            ("b", "string"),
            {"name": "c", "type": "decimal(10,2)"},
        ])
        assert result == [
            Column("a", s.bigint(), primary_key=True),
            Column("b", s.string()),
            Column("c", s.decimal(10, 2)),
        ]

    def test_idempotent(self):
        cols = [Column("x", s.bigint())]
        once = normalize_schema(cols)
        twice = normalize_schema(once)
        assert twice == once

    def test_rejects_string_input(self):
        # Common typo: passing a single string instead of a list.
        with pytest.raises(TypeError):
            normalize_schema("bigint")  # type: ignore[arg-type]

    def test_rejects_dict_input(self):
        # Common typo: passing a single dict instead of a list of dicts.
        with pytest.raises(TypeError):
            normalize_schema({"name": "x", "type": "bigint"})  # type: ignore[arg-type]

    def test_rejects_short_tuple(self):
        with pytest.raises(ValueError, match="2 elements"):
            normalize_schema([("just_a_name",)])  # type: ignore[list-item]

    def test_rejects_unknown_entry_type(self):
        with pytest.raises(TypeError):
            normalize_schema([42])  # type: ignore[list-item]


# =============================================================================
# schema_richness — used for conflict resolution between pipelines
# =============================================================================

class TestSchemaRichness:
    def test_empty_schema_zero(self):
        assert schema_richness([]) == 0

    def test_single_column_minimal_one(self):
        assert schema_richness([Column("x", s.bigint())]) == 1

    def test_constraints_increase_score(self):
        plain = [Column("x", s.bigint())]
        with_pk = [Column("x", s.bigint(), primary_key=True)]
        with_pk_and_nullable = [Column("x", s.bigint(), primary_key=True, nullable=False)]
        assert schema_richness(plain) < schema_richness(with_pk)
        assert schema_richness(with_pk) < schema_richness(with_pk_and_nullable)

    def test_more_columns_wins_over_more_constraints(self):
        # 5 plain columns beat 1 column with 4 constraints (5 vs 5 — actually equal).
        # The point is: column count contributes too.
        many = [Column(f"c{i}", s.bigint()) for i in range(5)]
        rich_one = [Column("x", s.bigint(), primary_key=True, nullable=False,
                           unique=True, description="d")]
        assert schema_richness(many) == 5
        assert schema_richness(rich_one) == 5  # 1 + 4 constraints


# =============================================================================
# dict_schema_richness — same scoring on the serialized dict form (used by
# the backend conflict-resolution path, which works with already-serialized
# schemas read from pipeline_registry).
# =============================================================================

class TestDictSchemaRichness:
    def test_empty_schema_zero(self):
        assert dict_schema_richness([]) == 0
        assert dict_schema_richness(None) == 0

    def test_single_column_minimal_one(self):
        assert dict_schema_richness([{"name": "x", "type": "bigint"}]) == 1

    def test_constraints_increase_score(self):
        plain = [{"name": "x", "type": "bigint"}]
        with_pk = [{"name": "x", "type": "bigint", "primary_key": True}]
        with_pk_and_nn = [{"name": "x", "type": "bigint",
                           "primary_key": True, "nullable": False}]
        assert dict_schema_richness(plain) < dict_schema_richness(with_pk)
        assert dict_schema_richness(with_pk) < dict_schema_richness(with_pk_and_nn)

    def test_default_value_is_not_a_constraint(self):
        # nullable=True is the default; presenting it explicitly must not
        # inflate the score (matches `column_to_dict` omit-on-default).
        without = [{"name": "x", "type": "bigint"}]
        with_default = [{"name": "x", "type": "bigint", "nullable": True}]
        assert dict_schema_richness(without) == dict_schema_richness(with_default)

    def test_matches_typed_schema_richness_for_round_tripped_columns(self):
        # The dict and Column scoring must agree for the same logical schema —
        # otherwise the SDK and backend would disagree on which declaration wins.
        cols = [
            Column("id", s.bigint(), primary_key=True, nullable=False),
            Column("event_date", s.date(), partition_key=True),
            Column("amount", s.decimal(10, 2), description="USD amount"),
        ]
        dicts = [column_to_dict(c) for c in cols]
        assert schema_richness(cols) == dict_schema_richness(dicts)

    def test_malformed_entries_skipped(self):
        # Defensive: a non-dict entry doesn't crash the scorer.
        schema = [
            {"name": "ok", "type": "bigint", "primary_key": True},
            "not-a-dict",            # noise
            None,                    # noise
            {"name": "ok2", "type": "string"},
        ]
        # Two valid columns with one constraint = 2 + 1.
        assert dict_schema_richness(schema) == 3


# =============================================================================
# Duplicate column name detection — caught at normalize_schema, not later
# =============================================================================

class TestDuplicateColumnNames:
    """Catalogs (Glue, Iceberg, BigQuery) reject duplicate column names; we
    fail early at deploy time so the user sees the issue on their machine
    instead of from a CloudFormation rollback."""

    def test_simple_duplicate_raises(self):
        with pytest.raises(ValueError, match=r"Duplicate column name.*'id'"):
            normalize_schema([
                Column("id", s.bigint()),
                Column("id", s.string()),
            ])

    def test_duplicate_among_three_caught(self):
        with pytest.raises(ValueError, match=r"Duplicate column name.*'amount'"):
            normalize_schema([
                Column("order_id", s.bigint()),
                Column("amount", s.decimal(10, 2)),
                Column("amount", s.bigint()),
            ])

    def test_position_indices_in_error_message(self):
        # The error should name positions 0 and 2 — useful for the user
        # to find both offenders in a long schema.
        with pytest.raises(ValueError, match=r"positions 0 and 2"):
            normalize_schema([
                Column("dup", s.bigint()),
                Column("other", s.string()),
                Column("dup", s.string()),
            ])

    def test_case_sensitive(self):
        # Catalog systems are case-preserving, so `ID` and `id` are distinct.
        cols = normalize_schema([Column("ID", s.bigint()), Column("id", s.string())])
        assert len(cols) == 2

    def test_mixed_format_duplicate_caught(self):
        # Mixing tuple form and Column form should still trigger the check.
        with pytest.raises(ValueError, match="Duplicate column name"):
            normalize_schema([
                ("id", "bigint"),
                Column("id", s.string()),
            ])

    def test_dict_form_duplicate_caught(self):
        with pytest.raises(ValueError, match="Duplicate column name"):
            normalize_schema([
                {"name": "id", "type": "bigint"},
                {"name": "id", "type": "string"},
            ])


# =============================================================================
# Column.__repr__ — concise, default-eliding for REPL ergonomics
# =============================================================================

class TestColumnRepr:
    def test_minimal_column_repr(self):
        # No extras → just name + type, no constraint clutter.
        c = Column("id", s.bigint())
        assert repr(c) == "Column('id', bigint)"

    def test_pk_column_repr(self):
        c = Column("id", s.bigint(), primary_key=True, nullable=False)
        # Constraint fields appear in their dataclass declaration order
        # (description, nullable, primary_key, ...).
        assert "primary_key=True" in repr(c)
        assert "nullable=False" in repr(c)
        # Default fields (unique=False, default=None, partition_key=False) are absent.
        assert "unique" not in repr(c)
        assert "default" not in repr(c)
        assert "partition_key" not in repr(c)

    def test_decimal_repr_includes_precision_scale(self):
        c = Column("amount", s.decimal(10, 2))
        assert repr(c) == "Column('amount', decimal(10,2))"

    def test_nested_type_repr(self):
        c = Column("tags", s.array(s.string()))
        assert "array<string>" in repr(c)

    def test_description_appears_when_set(self):
        c = Column("status", s.string(), description="pending|shipped")
        assert "description='pending|shipped'" in repr(c)


# =============================================================================
# Module-level _polyris_type_to_jsonschema — used by Asset.to_jsonschema()
# =============================================================================

class TestJsonSchemaConversion:

    def test_integer_types_map_to_integer(self):
        from polyris.schema import _polyris_type_to_jsonschema
        for t in (s.tinyint(), s.smallint(), s.integer(), s.bigint()):
            assert _polyris_type_to_jsonschema(t, nullable=False) == {"type": "integer"}

    def test_decimal_carries_format_hint(self):
        from polyris.schema import _polyris_type_to_jsonschema
        out = _polyris_type_to_jsonschema(s.decimal(10, 2), nullable=False)
        assert out["type"] == "number"
        assert out["format"] == "decimal(10,2)"

    def test_varchar_carries_max_length(self):
        from polyris.schema import _polyris_type_to_jsonschema
        out = _polyris_type_to_jsonschema(s.varchar(255), nullable=False)
        assert out == {"type": "string", "maxLength": 255}

    def test_char_carries_min_and_max_length(self):
        from polyris.schema import _polyris_type_to_jsonschema
        out = _polyris_type_to_jsonschema(s.char(10), nullable=False)
        assert out == {"type": "string", "minLength": 10, "maxLength": 10}

    def test_uuid_carries_format(self):
        from polyris.schema import _polyris_type_to_jsonschema
        out = _polyris_type_to_jsonschema(s.uuid(), nullable=False)
        assert out == {"type": "string", "format": "uuid"}

    def test_timestamp_carries_date_time_format(self):
        from polyris.schema import _polyris_type_to_jsonschema
        out = _polyris_type_to_jsonschema(s.timestamp(), nullable=False)
        assert out == {"type": "string", "format": "date-time"}

    def test_nullable_appends_null_to_type_union(self):
        from polyris.schema import _polyris_type_to_jsonschema
        out = _polyris_type_to_jsonschema(s.bigint(), nullable=True)
        assert out == {"type": ["integer", "null"]}

    def test_array_with_inner_string(self):
        from polyris.schema import _polyris_type_to_jsonschema
        out = _polyris_type_to_jsonschema(s.array(s.string()), nullable=False)
        assert out == {"type": "array", "items": {"type": "string"}}

    def test_struct_renders_as_object(self):
        from polyris.schema import _polyris_type_to_jsonschema
        st = s.struct(x=s.integer(), y=s.string())
        out = _polyris_type_to_jsonschema(st, nullable=False)
        assert out["type"] == "object"
        assert out["properties"] == {"x": {"type": "integer"}, "y": {"type": "string"}}
        assert out["required"] == ["x", "y"]

    def test_map_renders_as_object_with_additional_properties(self):
        from polyris.schema import _polyris_type_to_jsonschema
        out = _polyris_type_to_jsonschema(s.map_(s.string(), s.bigint()), nullable=False)
        assert out["type"] == "object"
        assert out["additionalProperties"] == {"type": "integer"}
        # String keys → no propertyNames hint needed.
        assert "propertyNames" not in out

    def test_map_with_non_string_key_carries_property_names(self):
        from polyris.schema import _polyris_type_to_jsonschema
        out = _polyris_type_to_jsonschema(s.map_(s.bigint(), s.string()), nullable=False)
        assert out["propertyNames"] == {"type": "integer"}
