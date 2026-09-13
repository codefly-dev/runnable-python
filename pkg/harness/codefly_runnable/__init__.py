"""The Codefly Python Runnable harness.

A generated runnable is a module with a ``handle(context, input) -> output``
function; this package validates what reaches it, runs it under the caller's
deadline and reports one unambiguous outcome.
"""

from .context import Context
from .protocol import HandlerFailure, InvocationIdentity, RunnableIdentity
from .schema import Schema, SchemaError

__all__ = ["HandlerFailure", "Context", "InvocationIdentity", "RunnableIdentity", "Schema", "SchemaError"]
