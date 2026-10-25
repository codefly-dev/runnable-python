"""The bounded schema profile both payloads of a call are held to.

Six value types, ``optional`` (the key may be absent) and ``nullable`` (the
value may be null) declared separately, and no coercion anywhere: a payload is
the shape the contract declares or the invocation does not run.

A ``file`` is a document. On the wire it is one closed object — ``reference``,
``media_type``, an optional ``name`` and the ``content`` in standard padded
base64 (core's ``runnable.FileValue``) — and the handler sees a :class:`File`
holding the bytes. The caller staged those bytes with its own authority; the
handler never receives a path or a URL, and what it returns is published by
the caller as a new document, or as a new version of an input it was handed.
"""

from __future__ import annotations

import base64
import binascii
import unicodedata
from dataclasses import dataclass, field as dataclass_field
from typing import Any

STRING = "string"
INTEGER = "integer"
BOOLEAN = "boolean"
OBJECT = "object"
ARRAY = "array"
FILE = "file"

TYPES = (STRING, INTEGER, BOOLEAN, OBJECT, ARRAY, FILE)

# The members of a file value, as core spells them (runnable.FileValue).
FILE_REFERENCE = "reference"
FILE_MEDIA_TYPE = "media_type"
FILE_NAME = "name"
FILE_CONTENT = "content"
FILE_MEMBERS = (FILE_REFERENCE, FILE_MEDIA_TYPE, FILE_NAME, FILE_CONTENT)

# Core's bounds on a file value's metadata (runnable.MaxFileReferenceBytes,
# runnable.MaxFileNameBytes). The content is bounded by the payload bound.
MAX_FILE_REFERENCE_BYTES = 1024
MAX_FILE_NAME_BYTES = 255

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1


class SchemaError(ValueError):
    """A payload does not satisfy the declared contract."""


@dataclass(frozen=True)
class File:
    """One document as a handler holds it.

    ``reference`` is the caller's opaque identity of the stored document: an
    input always carries one, and a handler may echo it on an output to say
    "this is a new version of that input" — never to name anything else. A
    returned file without a reference is a new document. ``content`` is the
    document's bytes.
    """

    media_type: str
    content: bytes
    reference: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class Field:
    """One field of the bounded profile."""

    name: str
    type: str
    optional: bool = False
    nullable: bool = False
    fields: tuple["Field", ...] = ()
    items: "Field | None" = None
    media_types: tuple[str, ...] = ()

    @staticmethod
    def parse(raw: dict[str, Any]) -> "Field":
        kind = raw.get("type")
        if kind not in TYPES:
            raise SchemaError(f"type {kind!r} is outside the bounded profile")
        media_types = tuple(raw.get("media-types", ()))
        if kind == FILE and not media_types:
            raise SchemaError(f"file {raw.get('name', '')!r} names no media types")
        return Field(
            name=raw.get("name", ""),
            type=kind,
            optional=bool(raw.get("optional", False)),
            nullable=bool(raw.get("nullable", False)),
            fields=tuple(Field.parse(f) for f in raw.get("fields", ())),
            items=Field.parse(raw["items"]) if raw.get("items") is not None else None,
            media_types=media_types,
        )


@dataclass(frozen=True)
class Schema:
    """The shape of one invocation payload; a payload is always an object."""

    fields: tuple[Field, ...] = dataclass_field(default=())

    @staticmethod
    def parse(raw: dict[str, Any] | None) -> "Schema":
        raw = raw or {}
        return Schema(fields=tuple(Field.parse(f) for f in raw.get("fields", ())))

    def validate(self, payload: Any, at: str, references: frozenset[str] | None = None) -> None:
        """Raise SchemaError unless payload satisfies this schema exactly.

        ``references`` says which side the payload is. ``None`` is an input:
        every file names the document it was read from. A set is an output:
        a file may name one of the call's input references — and only one of
        them — as the document it is a new version of.
        """
        _validate_object(self.fields, payload, at, references)

    def references(self, payload: Any) -> frozenset[str]:
        """The references of every file in a payload this schema admitted."""
        found: set[str] = set()
        _walk_files(self.fields, payload, lambda value: found.add(value[FILE_REFERENCE]))
        return frozenset(found)

    def to_handler(self, payload: Any) -> Any:
        """The admitted payload as a handler reads it: each file a :class:`File`."""
        return _map_files(self.fields, payload, _file_of)

    def to_wire(self, payload: Any) -> Any:
        """A handler's answer as the wire carries it: each :class:`File` an object.

        A value that is not a :class:`File` is left as it is, so validation
        names whatever the handler put where a file belongs.
        """
        return _map_files(self.fields, payload, _wire_of)


def _file_of(value: dict[str, Any]) -> File:
    return File(
        media_type=value[FILE_MEDIA_TYPE],
        content=base64.b64decode(value[FILE_CONTENT], validate=True),
        reference=value.get(FILE_REFERENCE),
        name=value.get(FILE_NAME),
    )


def _wire_of(value: Any) -> Any:
    if not isinstance(value, File):
        return value
    if not isinstance(value.content, (bytes, bytearray)):
        raise SchemaError("a returned File's content must be bytes")
    wire: dict[str, Any] = {
        FILE_MEDIA_TYPE: value.media_type,
        FILE_CONTENT: base64.b64encode(bytes(value.content)).decode("ascii"),
    }
    if value.reference is not None:
        wire[FILE_REFERENCE] = value.reference
    if value.name is not None:
        wire[FILE_NAME] = value.name
    return wire


def _map_files(fields: tuple[Field, ...], payload: Any, convert) -> Any:
    if not isinstance(payload, dict):
        return payload
    out = dict(payload)
    for field in fields:
        if field.name in out:
            out[field.name] = _map_value(field, out[field.name], convert)
    return out


def _map_value(field: Field, value: Any, convert) -> Any:
    if value is None:
        return None
    if field.type == FILE:
        return convert(value)
    if field.type == OBJECT:
        return _map_files(field.fields, value, convert)
    if field.type == ARRAY and isinstance(value, list):
        return [_map_value(field.items, element, convert) for element in value]
    return value


def _walk_files(fields: tuple[Field, ...], payload: Any, visit) -> None:
    def walk(field: Field, value: Any) -> None:
        if value is None:
            return
        if field.type == FILE:
            visit(value)
        elif field.type == OBJECT:
            _walk_files(field.fields, value, visit)
        elif field.type == ARRAY:
            for element in value:
                walk(field.items, element)

    for field in fields:
        if isinstance(payload, dict) and field.name in payload:
            walk(field, payload[field.name])


def _validate_object(fields: tuple[Field, ...], payload: Any, at: str, references: frozenset[str] | None) -> None:
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
        _validate_value(field, payload[field.name], f"{at}.{field.name}", references)


def _validate_value(field: Field, value: Any, at: str, references: frozenset[str] | None) -> None:
    if value is None:
        if field.nullable:
            return
        raise SchemaError(f"{at} is null but the contract does not declare it nullable")
    if field.type == OBJECT:
        _validate_object(field.fields, value, at, references)
        return
    if field.type == ARRAY:
        if not isinstance(value, list):
            raise SchemaError(f"{at} must be an array, got {_name(value)}")
        for index, element in enumerate(value):
            _validate_value(field.items, element, f"{at}[{index}]", references)
        return
    if field.type == FILE:
        _validate_file(field, value, at, references)
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


def _validate_file(field: Field, value: Any, at: str, references: frozenset[str] | None) -> None:
    """Hold one file value to core's rules (runnable.CheckFileValue)."""
    if not isinstance(value, dict):
        raise SchemaError(f"{at} must be a file object, got {_name(value)}")
    for key in value:
        if key not in FILE_MEMBERS:
            raise SchemaError(f"{at}.{key} is not a member of a file; a file carries {', '.join(FILE_MEMBERS)} and never a path")
    for key, member in value.items():
        if not isinstance(member, str):
            raise SchemaError(f"{at}.{key} must be a string, got {_name(member)}")
    if FILE_CONTENT not in value:
        raise SchemaError(f"{at}.{FILE_CONTENT} is required")
    content = value[FILE_CONTENT]
    try:
        decoded = base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError) as err:
        raise SchemaError(f"{at}.{FILE_CONTENT} is not standard padded base64: {err}") from None
    if base64.b64encode(decoded).decode("ascii") != content:
        raise SchemaError(f"{at}.{FILE_CONTENT} is not standard padded base64")
    if value.get(FILE_MEDIA_TYPE) not in field.media_types:
        raise SchemaError(f"{at}.{FILE_MEDIA_TYPE} {value.get(FILE_MEDIA_TYPE)!r} is not one of {list(field.media_types)}")
    name = value.get(FILE_NAME)
    if name is not None and not _admitted_name(name):
        raise SchemaError(f"{at}.{FILE_NAME} must be at most {MAX_FILE_NAME_BYTES} bytes of printable text without a path separator")
    reference = value.get(FILE_REFERENCE)
    if references is None:
        if reference is None or not _admitted_reference(reference):
            raise SchemaError(f"{at}.{FILE_REFERENCE} must name the document, in 1..{MAX_FILE_REFERENCE_BYTES} bytes without whitespace or control characters")
    elif reference is not None and reference not in references:
        raise SchemaError(f"{at}.{FILE_REFERENCE} names no input document of this call; a returned file is a new document or a new version of one the call was handed")


def _admitted_reference(reference: str) -> bool:
    if not reference or len(reference.encode("utf-8")) > MAX_FILE_REFERENCE_BYTES:
        return False
    return not any(c.isspace() or unicodedata.category(c) == "Cc" for c in reference)


def _admitted_name(name: str) -> bool:
    if len(name.encode("utf-8")) > MAX_FILE_NAME_BYTES or name in (".", ".."):
        return False
    return not any(c in "/\\" or unicodedata.category(c) == "Cc" for c in name)


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
