import pytest

from codefly_runnable.schema import Schema, SchemaError

TEXT = {"fields": [{"name": "text", "type": "string"}]}
COUNT = {"fields": [{"name": "count", "type": "integer"}]}
FLAG = {"fields": [{"name": "flag", "type": "boolean"}]}


def validate(raw, payload):
    Schema.parse(raw).validate(payload, "input")


def test_empty_object_schema_accepts_only_an_empty_object():
    validate({}, {})
    with pytest.raises(SchemaError, match="not declared"):
        validate({}, {"text": "x"})


def test_required_key_absent_is_rejected():
    with pytest.raises(SchemaError, match=r"input\.text is required"):
        validate(TEXT, {})


def test_optional_key_may_be_absent_but_not_null():
    schema = {"fields": [{"name": "text", "type": "string", "optional": True}]}
    validate(schema, {})
    with pytest.raises(SchemaError, match="nullable"):
        validate(schema, {"text": None})


def test_nullable_key_may_be_null_but_not_absent():
    schema = {"fields": [{"name": "text", "type": "string", "nullable": True}]}
    validate(schema, {"text": None})
    with pytest.raises(SchemaError, match="required"):
        validate(schema, {})


@pytest.mark.parametrize("value", ["3", 3.0, True, None, [], {}])
def test_integer_rejects_every_coercion(value):
    with pytest.raises(SchemaError):
        validate(COUNT, {"count": value})


@pytest.mark.parametrize("value", [2**63, -(2**63) - 1])
def test_integer_range_is_signed_64_bit(value):
    with pytest.raises(SchemaError, match="64-bit"):
        validate(COUNT, {"count": value})
    validate(COUNT, {"count": value // 2})


@pytest.mark.parametrize("value", [1, 0, "true"])
def test_boolean_rejects_integers_masquerading_as_booleans(value):
    with pytest.raises(SchemaError, match="must be a boolean"):
        validate(FLAG, {"flag": value})


def test_string_rejects_numbers():
    with pytest.raises(SchemaError, match="must be a string, got integer"):
        validate(TEXT, {"text": 1})


def test_nested_objects_and_arrays_report_their_path():
    schema = {
        "fields": [
            {
                "name": "options",
                "type": "object",
                "fields": [
                    {
                        "name": "stop_words",
                        "type": "array",
                        "items": {"type": "string"},
                    }
                ],
            }
        ]
    }
    validate(schema, {"options": {"stop_words": ["a", "b"]}})
    with pytest.raises(SchemaError, match=r"input\.options\.stop_words\[1\] must be a string"):
        validate(schema, {"options": {"stop_words": ["a", 2]}})


def test_array_of_objects_is_validated_element_by_element():
    schema = {
        "fields": [
            {
                "name": "rows",
                "type": "array",
                "items": {"type": "object", "fields": [{"name": "id", "type": "integer"}]},
            }
        ]
    }
    validate(schema, {"rows": [{"id": 1}, {"id": 2}]})
    with pytest.raises(SchemaError, match=r"input\.rows\[1\]\.id is required"):
        validate(schema, {"rows": [{"id": 1}, {}]})


def test_unknown_field_type_is_refused_at_parse_time():
    with pytest.raises(SchemaError, match="outside the bounded profile"):
        Schema.parse({"fields": [{"name": "x", "type": "number"}]})
