"""The generated contract the harness enforces at runtime."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .protocol import DEFAULT_MAX_LOG_BYTES, PROTOCOL, ProtocolError
from .schema import Schema

CONTRACT_FILE = "contract.json"
CONTRACT_SCHEMA = "codefly.runnable-generated-contract/v1"


@dataclass(frozen=True)
class Contract:
    """Everything the harness needs that the request does not carry."""

    handler_module: str
    handler_attribute: str
    input: Schema
    output: Schema
    recovery: str
    max_input_bytes: int
    max_output_bytes: int
    max_log_bytes: int
    runnable: dict

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
        return Contract(
            handler_module=document["handler"]["module"],
            handler_attribute=document["handler"]["attribute"],
            input=Schema.parse(document.get("input")),
            output=Schema.parse(document.get("output")),
            recovery=document["recovery"],
            runnable=document["runnable"],
            max_input_bytes=int(document["max-input-bytes"]),
            max_output_bytes=int(document["max-output-bytes"]),
            max_log_bytes=int(document.get("max-log-bytes", DEFAULT_MAX_LOG_BYTES)),
        )
