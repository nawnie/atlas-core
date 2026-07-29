from __future__ import annotations

from dataclasses import dataclass

from .models import ActionProposal, ApprovalStatus, RiskLevel


@dataclass(frozen=True)
class PolicyResult:
    status: ApprovalStatus
    reason: str


class ApprovalPolicy:
    """Deterministic safety gate. It never executes the proposed action."""

    def evaluate(self, proposal: ActionProposal) -> PolicyResult:
        if not proposal.payload:
            return PolicyResult(ApprovalStatus.BLOCKED, "Action payload is empty.")
        if proposal.risk in {RiskLevel.HIGH, RiskLevel.CRITICAL}:
            return PolicyResult(
                ApprovalStatus.BLOCKED,
                f"{proposal.risk.value} risk requires a separately configured policy.",
            )
        return PolicyResult(
            ApprovalStatus.PENDING,
            "Human review is required before execution.",
        )
