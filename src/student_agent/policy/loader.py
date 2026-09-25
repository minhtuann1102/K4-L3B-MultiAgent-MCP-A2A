"""Policy loader for reading MCP policy data and local defaults."""
from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any


def load_local_scoring_policy(root: Path | None = None) -> dict[str, Any]:
    """Load scoring-policy-v2.json if available."""
    target_root = root or Path.cwd()
    policy_path = target_root / "contracts" / "scoring" / "scoring-policy-v2.json"
    if policy_path.exists():
        with contextlib.suppress(Exception):
            return json.loads(policy_path.read_text(encoding="utf-8"))
    return {}


def load_policy_rules(policy_data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normalize policy rules from MCP response or fallback schema."""
    rules: dict[str, Any] = {
        "refund_eligible": True,
        "late_delivery_comp_rate": 0.0,
        "max_refund_limit_brl": None,
        "requires_seller_notification": True,
        "allow_partial_refund": True,
    }
    if not isinstance(policy_data, dict):
        return rules

    if "refund_eligible" in policy_data:
        rules["refund_eligible"] = bool(policy_data["refund_eligible"])
    if "eligible_for_refund" in policy_data:
        rules["refund_eligible"] = bool(policy_data["eligible_for_refund"])
    if "late_delivery_comp_rate" in policy_data:
        with contextlib.suppress(ValueError, TypeError):
            rules["late_delivery_comp_rate"] = float(policy_data["late_delivery_comp_rate"])
    if "max_refund_limit_brl" in policy_data:
        with contextlib.suppress(ValueError, TypeError):
            rules["max_refund_limit_brl"] = float(policy_data["max_refund_limit_brl"])
    return rules
