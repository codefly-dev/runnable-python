"""The bounded schema profile of ``codefly.runnable/v1``.

Five value types, ``optional`` (the key may be absent) and ``nullable`` (the
value may be null) declared separately, and no coercion anywhere: a payload is
the shape the contract declares or the invocation does not run.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any

STRING = "string"
INTEGER = "integer"
BOOLEAN = "boolean"
OBJECT = "object"
ARRAY = "array"

TYPES = (STRING, INTEGER, BOOLEAN, OBJECT, ARRAY)

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1


class SchemaError(ValueError):
    """A payload does not satisfy the declared contract."""


@dataclass(frozen=True)
class Field:
    """One field of the bounded profile."""

    name: str
    type: str
    optional: bool = False
    nullable: bool = False
    fields: tuple["Field", ...] = ()
    items: "Field | None" = None

    @staticmethod
    def parse(raw: dict[str, Any]) -> "Field":
        kind = raw.get("type")
        if kind not in TYPES:
            raise SchemaError(f"type {kind!r} is outside the bounded profile")
        return Field(
            name=raw.get("name", ""),
            type=kind,
            optional=bool(raw.get("optional", False)),
            nullable=bool(raw.get("nullable", False)),
            fields=tuple(Field.parse(f) for f in raw.get("fields", ())),
            items=Field.parse(raw["items"]) if raw.get("items") is not None else None,
        )


@dataclass(frozen=True)
class Schema:
    """The shape of one invocation payload; a payload is always an object."""

    fields: tuple[Field, ...] = dataclass_field(default=())

    @staticmethod
    def parse(raw: dict[str, Any] | None) -> "Schema":
        raw = raw or {}
        return Schema(fields=tuple(Field.parse(f) for f in raw.get("fields", ())))

    def validate(self, payload: Any, at: str) -> None:
        """Raise SchemaError unless payload satisfies this schema exactly."""
        _validate_object(self.fields, payload, at)


def _validate_object(fields: tuple[Field, ...], payload: Any, at: str) -> None:
    if not isinstance(payload, dict):
        raise SchemaError(f"{at} must be an object, got {_name(payload)}")
    declared = {f.name for f in fields}
    for key in payload:
        if key not in declared:
            raise SchemaError(f"{at}.{key} is not declared by the contract")
    for field in fields:
        if field.name not in payload:
            if field.optional:
                continue
            raise SchemaError(f"{at}.{field.name} is required")
        _validate_value(field, payload[field.name], f"{at}.{field.name}")


def _validate_value(field: Field, value: Any, at: str) -> None:
    if value is None:
        if field.nullable:
            return
        raise SchemaError(f"{at} is null but the contract does not declare it nullable")
    if field.type == OBJECT:
        _validate_object(field.fields, value, at)
        return
    if field.type == ARRAY:
        if not isinstance(value, list):
            raise SchemaError(f"{at} must be an array, got {_name(value)}")
        for index, element in enumerate(value):
            _validate_value(field.items, element, f"{at}[{index}]")
        return
    if field.type == BOOLEAN:
        if not isinstance(value, bool):
            raise SchemaError(f"{at} must be a boolean, got {_name(value)}")
        return
    if field.type == INTEGER:
        # bool is a subclass of int in Python: a boolean is never an integer here.
        if isinstance(value, bool) or not isinstance(value, int):
            raise SchemaError(f"{at} must be an integer, got {_name(value)}")
        if not INT64_MIN <= value <= INT64_MAX:
            raise SchemaError(f"{at} is outside the signed 64-bit integer range")
        return
    if not isinstance(value, str):
        raise SchemaError(f"{at} must be a string, got {_name(value)}")


def _name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__
