"""The generated contract the harness enforces at runtime."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .protocol import PROTOCOL, ProtocolError, RunnableIdentity
from .schema import Schema

CONTRACT_FILE = "contract.json"
CONTRACT_SCHEMA = "codefly.runnable-generated-contract/v1"

RECEIPT_ATTRIBUTE = "receipt_of"


@dataclass(frozen=True)
class Contract:
    """Everything the harness enforces that a call does not carry.

    It is read once, when the harness starts, rather than per call: the document
    is measured into the package digest, and a value re-read from disk between
    two calls would let one package answer under two contracts.
    """

    handler_module: str
    handler_attribute: str
    input: Schema
    output: Schema
    recovery: str
    max_input_bytes: int
    max_output_bytes: int
    timeout_seconds: float
    release: RunnableIdentity

    @staticmethod
    def load(path: Path) -> "Contract":
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("schema") != CONTRACT_SCHEMA:
            raise ProtocolError(
                f"{path} schema {document.get('schema')!r} is not {CONTRACT_SCHEMA!r}"
            )
        if document.get("protocol") != PROTOCOL:
            raise ProtocolError(
                f"{path} protocol {document.get('protocol')!r} is not {PROTOCOL!r}"
            )
        timeout = int(document["timeout-nanoseconds"])
        if timeout <= 0:
            raise ProtocolError(f"{path} declares no timeout, which bounds every call it answers")
        return Contract(
            handler_module=document["handler"]["module"],
            handler_attribute=document["handler"]["attribute"],
            input=Schema.parse(document.get("input")),
            output=Schema.parse(document.get("output")),
            recovery=document["recovery"],
            release=_release(document["runnable"]),
            max_input_bytes=int(document["max-input-bytes"]),
            max_output_bytes=int(document["max-output-bytes"]),
            timeout_seconds=timeout / 1_000_000_000,
        )


def _release(document: dict) -> RunnableIdentity:
    """The release this package implements.

    A field the generator left blank stays blank rather than making the harness
    refuse to start: the identity is what the handler is told it is running as,
    and standalone generation resolves no workspace.
    """
    return RunnableIdentity(**{key: str(document.get(key, "")) for key in
                               ("name", "module", "workspace", "version")})
