#!/usr/bin/env python
"""
Test the plan-panel and edit-and-rerun scaffolding (Milestone D).

Covers four areas:
    1. plan_outline() helper: gating, heading parse, fenced-code skip, slug.
    2. prefix_heading_ids(): id namespace prefix for h1/h2/h3.
    3. partials/message.html: side panel renders only when show_panel is True,
       with nav anchors that match bubble_id-slug.
    4. chat.html: rerun chip markup, edit button delegation hooks, mobile @media
       block keeps the panel stacking and hides the edit-btn-on-hover rule.

Each section prints a single PASS line on success or asserts.
Run from powerbuilder/ as:
    ./venv/bin/python scripts/_test_plan_panel_and_rerun.py
"""
from __future__ import annotations

import os
import sys

import django

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "powerbuilder_app.settings")
django.setup()

from django.template.loader import render_to_string

from chat.render_helpers import (
    plan_outline,
    prefix_heading_ids,
    is_plan_run,
)


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print(f"  OK   {msg}")


# ---------------------------------------------------------------------------
# 1. plan_outline()
# ---------------------------------------------------------------------------
print("\n[1] plan_outline()")

# Plan run with multiple headings: panel should show, sections preserved.
o = plan_outline(
    "# Strategy\n\n## Targeting\n\nbody\n\n## Messaging\n\n```\n# fenced heading should be skipped\n```\n\n## Cost\n",
    active_agents=["win_number", "precincts", "messaging", "researcher"],
    source_cards=[{"source": "Census", "date": "2024"}],
    downloads=[{"filename": "plan.docx"}, {"filename": "list.csv"}],
)
_assert(o["is_plan"] is True, "plan run flagged as plan")
_assert(o["show_panel"] is True, "panel shown when plan + structure present")
_assert([s["text"] for s in o["sections"]] == ["Strategy", "Targeting", "Messaging", "Cost"],
        "sections parsed in order, fenced heading skipped")
_assert(all(s["slug"] for s in o["sections"]), "every section has a slug")
_assert(o["sections"][2]["slug"] == "messaging", "slug is lowercased dash-joined ascii")
_assert(o["source_count"] == 1 and o["download_count"] == 2, "counts wired through")
_assert(o["agents"] == ["Win Number", "Precincts", "Messaging", "Researcher"],
        "agents prettified via agent_pill_label")

# Single-topic answer: not a plan, panel hidden.
o2 = plan_outline("# Win Number\n\n42,000.", active_agents=["win_number"])
_assert(o2["is_plan"] is False, "single-agent run is not a plan")
_assert(o2["show_panel"] is False, "panel hidden for single-topic answer")

# Plan-shape agents but only one heading: gated by has_structure.
o3 = plan_outline("# Plan\n\nbody only", active_agents=["win_number", "precincts", "messaging"])
_assert(o3["is_plan"] is True, "two plan agents = plan")
_assert(o3["show_panel"] is True, "3 agents alone are enough structure")

# Plan but only one heading and only two agents: panel hides.
o4 = plan_outline("# Plan\n\nbody only", active_agents=["win_number", "precincts"])
_assert(o4["show_panel"] is False, "1 heading + 2 agents = no panel")

# Empty / None inputs are defensive.
o5 = plan_outline("", active_agents=None)
_assert(o5["sections"] == [] and o5["agents"] == [], "empty inputs return empty lists")
_assert(o5["show_panel"] is False, "empty inputs hide panel")

# Slug stability across hyphens, digits, punctuation.
o6 = plan_outline(
    "## GA-07 win number, 2026 midterm!\n\n## Latinx 18-35 outreach",
    active_agents=["win_number", "precincts", "messaging"],
)
_assert(o6["sections"][0]["slug"] == "ga-07-win-number-2026-midterm",
        "slug strips punctuation, keeps hyphens and digits")
_assert(o6["sections"][1]["slug"] == "latinx-18-35-outreach", "slug works for second heading")


# ---------------------------------------------------------------------------
# 2. prefix_heading_ids()
# ---------------------------------------------------------------------------
print("\n[2] prefix_heading_ids()")

raw = '<h1 id="strategy">S</h1><h2 id="targeting">T</h2><h3 id="cost">C</h3><p>body</p>'
prefixed = prefix_heading_ids(raw, "b-abc")
_assert('id="b-abc-strategy"' in prefixed, "h1 id namespaced")
_assert('id="b-abc-targeting"' in prefixed, "h2 id namespaced")
_assert('id="b-abc-cost"' in prefixed, "h3 id namespaced")
_assert("<p>body</p>" in prefixed, "non-heading content untouched")

# Defensive: missing inputs.
_assert(prefix_heading_ids(None, "b-abc") == "", "None html returns empty string")
_assert(prefix_heading_ids(raw, "") == raw, "missing bubble_id returns input unchanged")

# h4 and beyond should NOT be prefixed (we only namespace the first three).
raw4 = '<h4 id="leave-me">x</h4>'
_assert(prefix_heading_ids(raw4, "b-x") == raw4, "h4+ left alone")


# ---------------------------------------------------------------------------
# 3. partials/message.html: plan panel render
# ---------------------------------------------------------------------------
print("\n[3] partials/message.html")

ctx_with_panel = {
    "answer_html": '<h1 id="b-x-strategy">Strategy</h1><h2 id="b-x-targeting">Targeting</h2><p>body</p>',
    "active_agents": ["win_number", "precincts", "messaging"],
    "downloads": [],
    "source_cards": [],
    "c3_footer": "Nonpartisan voter education. Not for candidate or party support.",
    "errors": [],
    "outline": plan_outline(
        "# Strategy\n\n## Targeting\n",
        active_agents=["win_number", "precincts", "messaging", "researcher"],
        source_cards=[{"source": "x", "date": "y"}],
        downloads=[{"filename": "p.docx"}],
    ),
    "bubble_id": "b-x",
}
html = render_to_string("partials/message.html", ctx_with_panel)
_assert('class="plan-panel"' in html, "plan panel rendered")
_assert('data-bubble-target="b-x"' in html, "panel keyed to bubble id")
_assert('data-action="toggle-panel"' in html, "panel collapse trigger present")
_assert('data-action="jump-section"' in html, "section nav links present")
_assert('href="#b-x-strategy"' in html, "first section anchor uses prefixed id")
_assert('href="#b-x-targeting"' in html, "second section anchor uses prefixed id")
_assert("plan-panel-pill" in html, "agent pills rendered in panel")
_assert(">1</strong> sources" in html, "source count rendered")
_assert(">1</strong> downloads" in html, "download count rendered")
_assert("bubble-asst--with-panel" in html, "bubble width modifier set when panel shown")

# Single-topic answer: panel must NOT render.
ctx_no_panel = {
    **ctx_with_panel,
    "outline": plan_outline("# Title\n", active_agents=["win_number"]),
    "active_agents": ["win_number"],
}
html_no = render_to_string("partials/message.html", ctx_no_panel)
_assert('class="plan-panel"' not in html_no, "no panel for single-topic answer")
_assert("bubble-asst--with-panel" not in html_no, "no bubble modifier when panel hidden")


# ---------------------------------------------------------------------------
# 4. chat.html: rerun chip markup + delegated handlers + mobile rules
# ---------------------------------------------------------------------------
print("\n[4] chat.html rerun chip + delegated handlers")

with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "templates", "chat.html"), "r", encoding="utf-8") as fh:
    chat_src = fh.read()

# Rerun chip markup is wired in.
_assert('id="rerun-chip"' in chat_src, "rerun chip element present")
_assert('id="rerun-clear"' in chat_src, "rerun cancel button present")
_assert("rerun-chip is-active" not in chat_src, "chip starts inert (toggled via JS)")

# Delegated event listener is wired on #messages and dispatches the three actions.
_assert("getElementById('messages').addEventListener('click'" in chat_src,
        "single delegated click listener on #messages")
_assert("'toggle-panel'" in chat_src, "toggle-panel action handled")
_assert("'jump-section'" in chat_src, "jump-section action handled")
_assert("'edit-rerun'" in chat_src, "edit-rerun action handled")

# escapeAttr helper exists for safely embedding the query into data-query.
_assert("function escapeAttr(text)" in chat_src, "escapeAttr helper defined")

# setRerunMode toggles state + chip visibility.
_assert("function setRerunMode(active, priorRow)" in chat_src, "setRerunMode defined")
_assert("classList.toggle('is-active', _rerunMode)" in chat_src, "chip toggled by mode")

# Live user bubble append now includes the edit button + data-query attr.
_assert('data-action="edit-rerun"' in chat_src, "live user bubble carries edit-rerun trigger")
_assert("escapeAttr(query)" in chat_src, "live append uses escapeAttr for the query attr")

# Splice-on-rerun logic: removes prior assistant rows before appending new one.
_assert("if (_rerunMode && _rerunPriorUserRow)" in chat_src,
        "rerun branch in submit handler")
_assert("_rerunPriorUserRow.remove();" in chat_src, "prior user row removed on rerun")

# Mobile media query keeps panel stacked and edit btn visible.
_assert(".msg-row.asst:has(.plan-panel)" in chat_src, "panel layout uses :has() selector")
_assert("flex: 1 1 100%;" in chat_src, "panel stacks full width on mobile")
_assert(".bubble-edit-btn { opacity: 0.85; }" in chat_src, "edit btn visible on touch")

# is_target highlight class wired in CSS.
_assert(".prose-dark h1.is-target" in chat_src, "section-jump highlight rule present")


# ---------------------------------------------------------------------------
# 5. is_plan_run gate stays consistent with plan_outline
# ---------------------------------------------------------------------------
print("\n[5] is_plan_run consistency")

_assert(is_plan_run(["win_number", "messaging"]) is True, "two plan agents qualifies")
_assert(is_plan_run(["win_number"]) is False, "one plan agent does not")
_assert(is_plan_run([]) is False, "empty list is not a plan")


print("\nplan-panel + edit-and-rerun test: ~36 assertions across 5 sections.")
print("PASS: all assertion groups OK.")
