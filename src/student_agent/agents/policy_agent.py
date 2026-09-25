"""Policy Agent for making policy-compliant business decisions."""
from __future__ import annotations

from typing import Any

from ..policy.engine import PolicyDecision, evaluate_policy


class PolicyAgent:
    """Evaluates case evidence against authoritative policies to produce a decision."""

    def decide(
        self,
        case: dict[str, Any],
        analysis: dict[str, Any],
        resolved_order_ids: list[str],
        has_errors: bool = False,
    ) -> PolicyDecision:
        return evaluate_policy(
            case=case,
            analysis=analysis,
            resolved_order_ids=resolved_order_ids,
            has_errors=has_errors,
        )
