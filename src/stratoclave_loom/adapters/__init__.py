"""Built-in agent backend adapters and the registry that resolves them."""

# Importing the modules below registers them with the global registry as a
# side effect.
# The Bedrock adapter is part of the standard registry but uses boto3
# only at call time, so importing it never fails even if the optional
# extra is not installed.
from stratoclave_loom.adapters import bedrock as bedrock
from stratoclave_loom.adapters import claude_code as claude_code
from stratoclave_loom.adapters import kiro_code as kiro_code
from stratoclave_loom.adapters import mock as mock
from stratoclave_loom.adapters._registry import (
    get_backend,
    list_backends,
    register_backend,
)

__all__ = [
    "get_backend",
    "list_backends",
    "register_backend",
]
