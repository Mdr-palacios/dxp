"""
Render-time helpers shared by the streaming and HTMX views.

extract_sources()  parses the researcher's "MEMO FROM SOURCE: ..." preamble
                   into a deduplicated list of source cards for the UI.
is_plan_run()      heuristic: did this run produce a multi-agent plan?
                   Used to decide whether to show the C3-safe footer.
agent_pill_label() prettifies internal node names ("opposition_research"
                   becomes "Opposition Research") for the agent pill row.
"""
from __future__ import annotations

import re
from typing import Iterable


# Researcher prepends each memo with:
#   --- MEMO FROM SOURCE: <source> | DATE: <date> ---
# This regex is anchored at line start so it does not match inside body text.
_MEMO_HEADER_RE = re.compile(
    r"^---\s*MEMO\s+FROM\s+SOURCE:\s*(.+?)\s*\|\s*DATE:\s*(.+?)\s*---\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# A short body preview shown when the user expands a source card. Long enough
# to give context, short enough to keep the UI tidy.
_PREVIEW_CHARS = 320


def extract_sources(research_results: Iterable[str] | None) -> list[dict]:
    """
    Return one card per unique research memo, in original order, deduped on
    (source, date). Each card has: source, date, preview.

    Defensive: an empty or None input returns []. Memos that don't match the
    expected header format are skipped rather than raised, the researcher
    occasionally returns a fallback string and we don't want one bad row to
    break the UI.
    """
    if not research_results:
        return []

    cards: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for memo in research_results:
        if not isinstance(memo, str):
            continue
        m = _MEMO_HEADER_RE.search(memo)
        if not m:
            continue
        source = m.group(1).strip()
        date = m.group(2).strip()
        key = (source.lower(), date.lower())
        if key in seen:
            continue
        seen.add(key)

        # Body is everything after the header line. Strip leading blank lines.
        body = memo[m.end():].lstrip()
        preview = body[:_PREVIEW_CHARS].rstrip()
        if len(body) > _PREVIEW_CHARS:
            preview += "\u2026"

        cards.append({
            "source":  source,
            "date":    date,
            "preview": preview,
        })

    return cards


# Active agents that, when present, indicate a full plan run rather than a
# single-topic answer. We surface the C3-safe disclosure only on plans,
# generic factual answers don't need it.
_PLAN_AGENTS = {"win_number", "precincts", "cost_calculator", "messaging"}


def is_plan_run(active_agents: Iterable[str] | None) -> bool:
    """
    Heuristic: a run is a "plan" if at least two of the plan-shape agents
    fired. One agent on its own is a single-topic answer (e.g. a quick
    win-number lookup, or messaging-only) and doesn't carry the same
    compliance weight.
    """
    if not active_agents:
        return False
    return len(_PLAN_AGENTS.intersection(active_agents)) >= 2


_C3_FOOTER_TEXT = (
    "Nonpartisan voter education. Not for candidate or party support."
)


def c3_footer_text() -> str:
    """Single source of truth for the C3-safe disclosure string."""
    return _C3_FOOTER_TEXT


def agent_pill_label(node_name: str) -> str:
    """
    Prettify an internal node name for display in the agent pill row.
    Examples:
        researcher           becomes Researcher
        opposition_research  becomes Opposition Research
        cost_calculator      becomes Cost Calculator
    """
    if not node_name:
        return ""
    return node_name.replace("_", " ").title()
