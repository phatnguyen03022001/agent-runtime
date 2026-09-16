from __future__ import annotations


class RuntimeValidationError(ValueError):
    """Caller- or operator-correctable Runtime validation failure."""


class RuntimeStateError(RuntimeError):
    """Caller-correctable Runtime state conflict."""


class RuntimeCapacityError(RuntimeStateError):
    """A heavy execution request exceeded the process-local hard limit."""
