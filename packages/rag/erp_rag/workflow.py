"""Deterministic query workflow state machine with durable checkpoints.

Only application code chooses transitions. Checkpoints store IDs, counts and reason codes — never
document text, questions or answers.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from erp_observability.redaction import redact
from erp_rag.stores.dynamo import JsonTable


class Stage(StrEnum):
    RECEIVED = "RECEIVED"
    PLANNED = "PLANNED"
    RETRIEVED = "RETRIEVED"
    AUTHORIZED = "AUTHORIZED"
    RERANKED = "RERANKED"
    PACKED = "PACKED"
    SUFFICIENCY_CHECKED = "SUFFICIENCY_CHECKED"
    GENERATED = "GENERATED"
    CITATIONS_VALIDATED = "CITATIONS_VALIDATED"
    JUDGED = "JUDGED"
    RELEASED = "RELEASED"
    ABSTAINED = "ABSTAINED"
    FAILED = "FAILED"


TERMINAL = frozenset({Stage.RELEASED, Stage.ABSTAINED, Stage.FAILED})

TRANSITIONS: dict[Stage, frozenset[Stage]] = {
    Stage.RECEIVED: frozenset({Stage.PLANNED, Stage.RELEASED}),  # RELEASED: validated cache hit
    Stage.PLANNED: frozenset({Stage.RETRIEVED}),
    Stage.RETRIEVED: frozenset({Stage.AUTHORIZED}),
    Stage.AUTHORIZED: frozenset({Stage.RERANKED, Stage.SUFFICIENCY_CHECKED}),
    Stage.RERANKED: frozenset({Stage.PACKED}),
    Stage.PACKED: frozenset({Stage.SUFFICIENCY_CHECKED}),
    # Insufficient evidence → bounded retrieval retry (back to PLANNED) or abstain.
    Stage.SUFFICIENCY_CHECKED: frozenset({Stage.GENERATED, Stage.PLANNED, Stage.ABSTAINED}),
    Stage.GENERATED: frozenset({Stage.CITATIONS_VALIDATED}),
    # After validation: judge, release (standard mode w/o sync judge), repair, or abstain.
    Stage.CITATIONS_VALIDATED: frozenset({Stage.JUDGED, Stage.RELEASED, Stage.GENERATED, Stage.ABSTAINED}),
    # Judge says revise → at most one repair (GENERATED); otherwise release/abstain.
    Stage.JUDGED: frozenset({Stage.GENERATED, Stage.RELEASED, Stage.ABSTAINED}),
}
for _s in Stage:
    if _s not in TERMINAL:
        TRANSITIONS[_s] = TRANSITIONS.get(_s, frozenset()) | {Stage.FAILED}


class IllegalTransitionError(RuntimeError):
    pass


class WorkflowRun:
    def __init__(self, table: JsonTable | None, *, trace_id: str, kind: str = "query") -> None:
        self.run_id = f"run_{secrets.token_hex(12)}"
        self.trace_id = trace_id
        self.kind = kind
        self.stage = Stage.RECEIVED
        self.history: list[Stage] = [Stage.RECEIVED]
        self.retrieval_retries = 0
        self.repair_cycles = 0
        self._seq = 0
        self._table = table

    async def start(self, **summary: Any) -> None:
        await self._checkpoint(Stage.RECEIVED, summary)

    async def advance(self, to: Stage, **summary: Any) -> None:
        if to not in TRANSITIONS.get(self.stage, frozenset()):
            raise IllegalTransitionError(f"{self.stage} -> {to}")
        if to == Stage.PLANNED and self.stage == Stage.SUFFICIENCY_CHECKED:
            self.retrieval_retries += 1
        if to == Stage.GENERATED and self.stage in (Stage.JUDGED, Stage.CITATIONS_VALIDATED):
            self.repair_cycles += 1
        self.stage = to
        self.history.append(to)
        await self._checkpoint(to, summary)

    async def _checkpoint(self, stage: Stage, summary: dict[str, Any]) -> None:
        self._seq += 1
        if self._table is None:
            return
        await self._table.put(
            {"run_id": self.run_id, "step": f"{self._seq:03d}:{stage}"},
            {
                "kind": self.kind,
                "trace_id": self.trace_id,
                "stage": stage,
                "at": datetime.now(UTC).isoformat(),
                "summary": redact(summary),
            },
            ttl=30 * 24 * 3600,
        )
