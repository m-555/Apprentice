"""Daily spend caps, shared by the MCP tools and the interactive/headless agent.

Two cap styles per provider (0/absent = no cap), counted from UTC midnight over
metrics.jsonl — the same data the §6.5 report reads:

    metering.budgets.<provider>_tokens_per_day   (tokens_out)
    metering.budgets.<provider>_usd_per_day      (est_cost_usd; needs providers.<p>.cost)

`exceeded()` returns a human-readable refusal string, or "" when the provider may run.
Callers turn that into whatever their surface needs (a tool error, a REPL message).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

try:
    from . import metering
except ImportError:
    import metering


def exceeded(cfg: dict[str, Any], provider: str) -> str:
    """Refusal message if `provider` is over a configured daily budget, else ""."""
    budgets = cfg.get("metering", {}).get("budgets", {})
    day_start = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00")

    tok_budget = int(budgets.get(f"{provider}_tokens_per_day", 0) or 0)
    if tok_budget > 0:
        used = metering.tier_token_total(provider, since_iso=day_start)
        if used >= tok_budget:
            return (f"Daily token budget for provider '{provider}' is exhausted: {used}/"
                    f"{tok_budget} tokens_out since UTC midnight. Use another provider "
                    f"(e.g. a local/free one) or raise "
                    f"metering.budgets.{provider}_tokens_per_day in config/qwen.local.json.")

    usd_budget = float(budgets.get(f"{provider}_usd_per_day", 0) or 0)
    if usd_budget > 0:
        spent = metering.tier_cost_total(provider, since_iso=day_start)
        if spent >= usd_budget:
            return (f"Daily USD budget for provider '{provider}' is exhausted: "
                    f"${spent:.4f}/${usd_budget:.2f} since UTC midnight. Use another "
                    f"provider (e.g. a local/free one) or raise "
                    f"metering.budgets.{provider}_usd_per_day in config/qwen.local.json.")
    return ""
