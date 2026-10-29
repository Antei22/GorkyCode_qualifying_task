"""Dependency boundaries for the planned flow. No SQL/LLM implementations yet."""
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.domain.operations import (
    ChangePlan, ExecutionResult, Interpretation, OperationIntent, OperationOutcome,
    PendingOperation, PolicyDecision, RecordSnapshot, TargetResolution,
)


@dataclass(frozen=True)
class TurnContext:
    owner_id: UUID
    request_id: UUID
    message: str
    now: datetime  # Inject from Clock, including timezone; never use dataset dates.
    history: tuple[tuple[str, str], ...] = ()


class IntentInterpreter(Protocol):
    def interpret(self, context: TurnContext, pending: PendingOperation | None) -> Interpretation:
        """Extract a proposal without storage writes or execution tool calls."""
        ...


class TargetResolver(Protocol):
    def resolve(self, context: TurnContext, intent: OperationIntent,
                selected_id: UUID | None = None) -> TargetResolution:
        """Owner-scoped search, explicit time filters and chronological ranking.

        Candidate selection must be checked against persisted candidates; a model
        returning an arbitrary UUID does not authorize accessing that record.
        When selected_id is supplied, resolve that exact record with the same owner
        and status checks instead of hoping it is still in a limited search page.
        """
        ...


class ChangePlanner(Protocol):
    def prepare(self, context: TurnContext, intent: OperationIntent,
                target: RecordSnapshot | None,
                previous_plan: ChangePlan | None = None) -> ChangePlan:
        """Resolve dates, merge the complete patch, preserve duration provenance.

        Create allocates an identity; update preserves the existing identity;
        cancellation changes status. Use domain.make_change_plan so the digest is
        canonical. During a clarification/confirmation, previous_plan lets create
        preserve its allocated identity. No operation is implemented as delete/create.
        """
        ...


class OperationPolicy(Protocol):
    def evaluate(self, context: TurnContext, intent: OperationIntent,
                 plan: ChangePlan | None, pending: PendingOperation | None) -> PolicyDecision:
        """Check current-user intent, missing fields and final-state invariants.

        Validate kind rules, date/time, duration precision, overlaps and permissions
        together. Confirmation is bound to the plan fingerprint and record revision.
        Cancellation of past records is rejected; historical reads remain allowed.
        """
        ...


class OperationExecutor(Protocol):
    def execute(self, context: TurnContext, plan: ChangePlan) -> ExecutionResult:
        """Called only after READY. Recheck owner/revision/conflicts atomically.

        The future SQL adapter shares Service's transaction. It must not open or
        commit an independent transaction or accept a plan directly from the LLM.
        """
        ...


class OutcomePresenter(Protocol):
    def present(self, context: TurnContext, outcome: OperationOutcome) -> str:
        """Render persisted facts, safe questions and server-owned error messages.

        Variations from a model may rephrase questions, but cannot claim execution
        without ExecutionResult or convert estimates into exact/actual durations.
        """
        ...
