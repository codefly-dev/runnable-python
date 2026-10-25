"""Documents cross the call as documents: a reference, a media type and bytes.

The handler reads each input file as a File holding the bytes the caller
staged, and what it returns is a new document or a new version of an input it
was handed. No path and no URL crosses in either direction.
"""

import base64

import pytest

from codefly_runnable.schema import File, Schema, SchemaError

PDF = {"fields": [{"name": "pdf", "type": "file", "media-types": ["application/pdf"]}]}
CONTENT = base64.b64encode(b"%PDF-1.7 fake").decode("ascii")
INPUT = {"pdf": {"reference": "store://t/entry/e1@v3", "media_type": "application/pdf", "name": "q3.pdf", "content": CONTENT}}


def test_an_input_file_reaches_the_handler_as_bytes():
    schema = Schema.parse(PDF)
    schema.validate(INPUT, "input")
    handed = schema.to_handler(INPUT)["pdf"]
    assert handed == File(media_type="application/pdf", content=b"%PDF-1.7 fake", reference="store://t/entry/e1@v3", name="q3.pdf")
    assert schema.references(INPUT) == frozenset({"store://t/entry/e1@v3"})


@pytest.mark.parametrize(
    "value, message",
    [
        ({"media_type": "application/pdf", "content": CONTENT}, "must name the document"),
        ({"reference": "a b", "media_type": "application/pdf", "content": CONTENT}, "must name the document"),
        ({"reference": "r", "media_type": "text/html", "content": CONTENT}, "is not one of"),
        ({"reference": "r", "media_type": "application/pdf", "content": CONTENT, "path": "/tmp/q3.pdf"}, "never a path"),
        ({"reference": "r", "media_type": "application/pdf", "content": CONTENT, "url": "https://x"}, "never a path"),
        ({"reference": "r", "media_type": "application/pdf", "name": "../q3.pdf", "content": CONTENT}, "without a path separator"),
        ({"reference": "r", "media_type": "application/pdf", "content": CONTENT.rstrip("=")}, "base64"),
        ({"reference": "r", "media_type": "application/pdf", "content": "JVBE\nRi0x"}, "base64"),
        ({"reference": "r", "media_type": "application/pdf"}, "content is required"),
        ({"reference": 7, "media_type": "application/pdf", "content": CONTENT}, "must be a string"),
        ("store://t/entry/e1", "must be a file object"),
    ],
)
def test_an_input_file_is_held_to_cores_file_value(value, message):
    with pytest.raises(SchemaError, match=message):
        Schema.parse(PDF).validate({"pdf": value}, "input")


def test_a_returned_file_is_a_new_document_or_a_version_of_an_input():
    schema = Schema.parse(PDF)
    handed = frozenset({"store://t/entry/e1@v3"})

    new_document = schema.to_wire({"pdf": File(media_type="application/pdf", content=b"new")})
    schema.validate(new_document, "output", handed)
    assert "reference" not in new_document["pdf"]

    new_version = schema.to_wire({"pdf": File(media_type="application/pdf", content=b"v4", reference="store://t/entry/e1@v3")})
    schema.validate(new_version, "output", handed)

    elsewhere = schema.to_wire({"pdf": File(media_type="application/pdf", content=b"x", reference="store://t/entry/other")})
    with pytest.raises(SchemaError, match="no input document of this call"):
        schema.validate(elsewhere, "output", handed)


def test_a_file_field_names_its_media_types():
    with pytest.raises(SchemaError, match="names no media types"):
        Schema.parse({"fields": [{"name": "doc", "type": "file"}]})
