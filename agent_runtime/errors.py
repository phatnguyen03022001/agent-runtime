from __future__ import annotations

from .contracts import sanitize_failure_message
from .tool_contract import (
    ContractError,
    ContractErrorCode,
    EffectState,
    SafeNextAction,
)


def annotate_failure(
    exc: Exception,
    *,
    code: ContractErrorCode,
    reason_code: str,
    message: str,
    retryable: bool,
    effect_state: EffectState,
    reconciliation_required: bool,
    safe_next_action: SafeNextAction,
) -> Exception:
    """Attach validated public failure semantics without changing exception identity."""

    ContractError(
        code=code,
        reason_code=reason_code,
        retryable=retryable,
        effect_state=effect_state,
        reconciliation_required=reconciliation_required,
        safe_next_action=safe_next_action,
    )
    exc.contract_code = code  # type: ignore[attr-defined]
    exc.reason_code = reason_code  # type: ignore[attr-defined]
    exc.message = sanitize_failure_message(message)  # type: ignore[attr-defined]
    exc.retryable = retryable  # type: ignore[attr-defined]
    exc.effect_state = effect_state  # type: ignore[attr-defined]
    exc.reconciliation_required = reconciliation_required  # type: ignore[attr-defined]
    exc.safe_next_action = safe_next_action  # type: ignore[attr-defined]
    return exc


class RuntimeValidationError(ValueError):
    """Caller- or operator-correctable Runtime validation failure."""

    def __init__(
        self,
        message: str,
        *,
        code: ContractErrorCode = ContractErrorCode.INVALID_ARGUMENT,
        reason_code: str = "INVALID_REQUEST",
    ) -> None:
        clean = sanitize_failure_message(message)
        ContractError(
            code=code,
            reason_code=reason_code,
            retryable=False,
            effect_state=EffectState.ABSENT,
            reconciliation_required=False,
            safe_next_action=SafeNextAction.FIX_REQUEST,
        )
        super().__init__(clean)
        self.code = code
        self.reason_code = reason_code
        self.message = clean
        self.retryable = False
        self.effect_state = EffectState.ABSENT
        self.reconciliation_required = False
        self.safe_next_action = SafeNextAction.FIX_REQUEST


class RuntimeStateError(RuntimeError):
    """Caller-correctable Runtime state conflict."""

    def __init__(
        self,
        message: str,
        *,
        code: ContractErrorCode = ContractErrorCode.PRECONDITION_FAILED,
        reason_code: str = "STATE_PRECONDITION_FAILED",
    ) -> None:
        clean = sanitize_failure_message(message)
        ContractError(
            code=code,
            reason_code=reason_code,
            retryable=False,
            effect_state=EffectState.ABSENT,
            reconciliation_required=False,
            safe_next_action=SafeNextAction.FIX_REQUEST,
        )
        super().__init__(clean)
        self.code = code
        self.reason_code = reason_code
        self.message = clean
        self.retryable = False
        self.effect_state = EffectState.ABSENT
        self.reconciliation_required = False
        self.safe_next_action = SafeNextAction.FIX_REQUEST


class RuntimeCapacityError(RuntimeStateError):
    """A heavy execution request exceeded a pre-dispatch process-local hard limit."""

    def __init__(self, message: str) -> None:
        clean = sanitize_failure_message(message)
        RuntimeError.__init__(self, clean)
        self.code = ContractErrorCode.LIMIT_EXCEEDED
        self.reason_code = "CAPACITY_EXHAUSTED"
        self.message = clean
        self.retryable = True
        self.effect_state = EffectState.ABSENT
        self.reconciliation_required = False
        self.safe_next_action = SafeNextAction.WAIT
