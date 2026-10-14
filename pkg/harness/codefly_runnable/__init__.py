"""The Codefly Python Runnable harness.

A generated runnable is a module with a ``handle(context, input) -> output``
function; this package serves it. One call is one POST carrying the bounded
input document, validated against the contract before the handler sees it and
validated again on the way out.
"""

from .context import Context
from .protocol import HandlerFailure, InvocationIdentity, RunnableIdentity
from .schema import Schema, SchemaError

__all__ = ["HandlerFailure", "Context", "InvocationIdentity", "RunnableIdentity", "Schema", "SchemaError"]
