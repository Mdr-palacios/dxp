# powerbuilder/chat/agents/opposition_research.py
"""
Opposition Research agent — retrieves Republican candidate research books from
the American Bridge Research Books MCP server and formats findings into a
structured memo for downstream use by the Messaging agent.

Search strategy — five-step cascading fallback (name-first):
  The MCP server exposes a keyword/content search tool (search_all), not a
  structured lookup.  Short district codes like "VA-07" return zero results.
  All queries must use candidate names or office/state keyword phrases.

  Step 1  Incumbent name  — only when election_results provides the opponent's
                            name; _search_by_name() returns the full research
                            content directly.  Result is used as-is; no
                            candidate-list matching step is needed.
  Step 2  State House sweep — congressional races only; _search_by_office()
                            returns a list of all House candidates in the state.
                            _find_matching_candidate() picks the correct
                            district; _search_by_name() then fetches full text.
  Step 3  Governor fallback — _search_by_office() returns research content
                            directly; _find_matching_candidate() extracts the
                            name for the memo header.
  Step 4  Senate fallback  — same pattern as Step 3 for U.S. Senate.
  Step 5  Trump impacts    — always available; Trump state-level policy impacts
                            as the broadest possible fallback.  Returns empty
                            only if the MCP server itself is unreachable.

  Steps 3-5 always insert a fallback_note so readers know the research is a
  proxy, not a direct match for the requested race.

MCP client selection:
  openai    → OpenAI SDK Responses API with native MCP tool support
  anthropic → Anthropic SDK beta messages with mcp_servers parameter
  all others (Gemini, Cohere, Groq, Mistral, Llama, ChangeAgent, unknown)
            → Anthropic SDK used as the MCP retrieval layer regardless of
              LLM_PROVIDER, ensuring equal access to opposition research
              across all configured providers.

Context is read from AgentState in this priority order:
  1. election_results structured_data — most complete; includes incumbent name
  2. Any other structured_data entry with state_fips + district_type + district_id
  3. Raw query string as last resort
"""

import logging
import os
from typing import Any, Optional

from dotenv import load_dotenv
load_dotenv()

from .state import AgentState
from ..utils.llm_config import LLM_PROVIDER

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RESEARCH_BOOKS_MCP_URL = "https://mcp.research-books.com/mcp"

# Providers that have their own native MCP API — all others fall back to Anthropic.
_NATIVE_MCP_PROVIDERS = {"openai", "anthropic"}

# State FIPS code → two-letter postal abbreviation (for candidate matching)
_FIPS_TO_STATE_ABBR: dict[str, str] = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA",
    "08": "CO", "09": "CT", "10": "DE", "11": "DC", "12": "FL",
    "13": "GA", "15": "HI", "16": "ID", "17": "IL", "18": "IN",
    "19": "IA", "20": "KS", "21": "KY", "22": "LA", "23": "ME",
    "24": "MD", "25": "MA", "26": "MI", "27": "MN", "28": "MS",
    "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH",
    "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND",
    "39": "OH", "40": "OK", "41": "OR", "42": "PA", "44": "RI",
    "45": "SC", "46": "SD", "47": "TN", "48": "TX", "49": "UT",
    "50": "VT", "51": "VA", "53": "WA", "54": "WV", "55": "WI",
    "56": "WY",
}

_STATE_ABBR_TO_NAME: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine",
    "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska",
    "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}


# ---------------------------------------------------------------------------
# MCP client factory
# ---------------------------------------------------------------------------

def _get_mcp_client() -> tuple[Any, str]:
    """
    Returns (sdk_client, provider_type) where provider_type is "openai" or
    "anthropic".  The provider_type tells the caller which calling convention
    to use — it does NOT necessarily match LLM_PROVIDER.

    - LLM_PROVIDER == "openai"     → OpenAI SDK, provider_type="openai"
    - LLM_PROVIDER == "anthropic"  → Anthropic SDK, provider_type="anthropic"
    - any other provider           → Anthropic SDK (fallback), provider_type="anthropic"
    """
    active = LLM_PROVIDER.lower().strip()

    if active == "openai":
        from openai import OpenAI
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY is required for the OpenAI MCP client.")
        return OpenAI(api_key=key), "openai"

    # Anthropic (native) or Anthropic fallback for all other providers
    import anthropic
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError(
            f"ANTHROPIC_API_KEY is required for MCP retrieval "
            f"(used as fallback for provider '{active}')."
        )
    return anthropic.Anthropic(api_key=key), "anthropic"


# ---------------------------------------------------------------------------
# MCP query helpers
# ---------------------------------------------------------------------------

def _query_mcp(client: Any, provider_type: str, prompt: str, timeout: int = 45) -> str:
    """
    Send a natural-language prompt to the Research Books MCP server.
    The server resolves tool calls server-side; the final assistant text
    is returned as a plain string.
    """
    if provider_type == "openai":
        # OpenAI Responses API — MCP tools are declared in the tools array;
        # the API connects to the server and resolves tool calls before returning.
        response = client.responses.create(
            model="gpt-4o",
            tools=[{
                "type":             "mcp",
                "server_label":     "research-books",
                "server_url":       RESEARCH_BOOKS_MCP_URL,
                "require_approval": "never",
                "headers": {
                    "Accept":       "application/json",
                    "Content-Type": "application/json",
                },
            }],
            input=prompt,
            timeout=timeout,
        )
        parts: list[str] = []
        for item in (response.output or []):
            # OutputTextItem has .text directly; some versions nest under .content
            text = getattr(item, "text", None)
            if text:
                parts.append(text)
                continue
            for block in (getattr(item, "content", None) or []):
                t = getattr(block, "text", None)
                if t:
                    parts.append(t)
        return "\n".join(parts).strip()

    else:  # anthropic — native or fallback for all other providers
        # Anthropic beta messages with remote MCP server.
        # mcp_servers connects to the URL; tool calls are resolved server-side.
        response = client.beta.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8192,
            mcp_servers=[{
                "type": "url",
                "url":  RESEARCH_BOOKS_MCP_URL,
                "name": "research-books",
                "headers": {
                    "Accept":       "application/json",
                    "Content-Type": "application/json",
                },
            }],
            messages=[{"role": "user", "content": prompt}],
            betas=["mcp-client-2025-04-04"],
            timeout=timeout,
        )
        parts = []
        for block in (response.content or []):
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        return "\n".join(parts).strip()


# ---------------------------------------------------------------------------
# District context extraction
# ---------------------------------------------------------------------------

def _to_office_label(district_type: str) -> str:
    return {
        "congressional": "U.S. House",
        "senate":        "U.S. Senate",
        "governor":      "Governor",
        "state_senate":  "State Senate",
        "state_house":   "State House",
    }.get(district_type.lower(), "Unknown")


def _build_district_label(state_abbr: str, district_type: str, district_id: str) -> str:
    dt = district_type.lower()
    if dt == "senate":
        return f"{state_abbr} U.S. Senate"
    if dt == "governor":
        return f"{state_abbr} Governor"
    if dt == "congressional" and district_id and district_id not in ("statewide", ""):
        try:
            dist_num = int(district_id[len(district_id) - len(district_id.lstrip("0123456789")):]
                          .lstrip() or district_id)
            # district_id is a GEOID like "5107"; strip state prefix to get district num
            state_prefix_len = 2  # state FIPS is always 2 digits in GEOID
            dist_num = int(district_id[state_prefix_len:])
            return f"{state_abbr}-{dist_num:02d} (U.S. House)"
        except (ValueError, IndexError):
            pass
    return f"{state_abbr} {district_type} {district_id}".strip()


def _detect_office_from_query(query: str) -> str:
    q = query.lower()
    if any(k in q for k in ("governor", "gubernatorial")):
        return "Governor"
    if any(k in q for k in ("u.s. senate", "senate race", "senate seat", "senator")):
        return "U.S. Senate"
    return "U.S. House"


def _extract_state_from_query(query: str) -> tuple[str, str]:
    """
    Scan a raw query string for a US state name or abbreviation.
    Returns (state_abbr, state_fips) or ("", "") if none found.
    """
    _ABBR_TO_FIPS: dict[str, str] = {v: k for k, v in _FIPS_TO_STATE_ABBR.items()}
    _NAME_TO_ABBR: dict[str, str] = {
        "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
        "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
        "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
        "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
        "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
        "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
        "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
        "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
        "new mexico": "NM", "new york": "NY", "north carolina": "NC",
        "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
        "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
        "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
        "vermont": "VT", "virginia": "VA", "washington": "WA",
        "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
        "district of columbia": "DC",
    }
    q = query.lower()
    # Check full state names first (longer matches take priority)
    for name in sorted(_NAME_TO_ABBR, key=len, reverse=True):
        if name in q:
            abbr = _NAME_TO_ABBR[name]
            return abbr, _ABBR_TO_FIPS.get(abbr, "")
    # Check two-letter abbreviations as whole words
    import re
    for abbr, fips in _ABBR_TO_FIPS.items():
        if re.search(rf"\b{abbr}\b", query.upper()):
            return abbr, fips
    return "", ""


def _extract_district_context(state: AgentState) -> Optional[dict]:
    """
    Extract district and candidate context in priority order:
      1. election_results structured_data — includes incumbent name from Cook
      2. Any structured_data entry with geographic keys
      3. Raw query string as fallback when structured_data is not yet populated
    Returns None only when no context can be derived at all.
    """
    structured = state.get("structured_data", [])

    # Priority 1: election_results output (richest context)
    er = next((d for d in structured if d.get("agent") == "election_results"), None)
    if er:
        fips      = str(er.get("state_fips", "")).zfill(2)
        abbr      = _FIPS_TO_STATE_ABBR.get(fips, "")
        dist_type = er.get("district_type", "")
        dist_id   = er.get("district_id", "")
        return {
            "state_fips":     er.get("state_fips", ""),
            "state_abbr":     abbr,
            "district_type":  dist_type,
            "district_id":    dist_id,
            "incumbent":      er.get("incumbent"),
            "office_type":    _to_office_label(dist_type),
            "district_label": _build_district_label(abbr, dist_type, dist_id),
        }

    # Priority 2: any other structured_data with geographic keys
    prior = next(
        (d for d in structured
         if d.get("state_fips") and d.get("district_type") and d.get("district_id")),
        None,
    )
    if prior:
        fips      = str(prior.get("state_fips", "")).zfill(2)
        abbr      = _FIPS_TO_STATE_ABBR.get(fips, "")
        dist_type = prior.get("district_type", "")
        dist_id   = prior.get("district_id", "")
        return {
            "state_fips":    prior.get("state_fips", ""),
            "state_abbr":    abbr,
            "district_type": dist_type,
            "district_id":   dist_id,
            "incumbent":     None,
            "office_type":   _to_office_label(dist_type),
            "district_label": _build_district_label(abbr, dist_type, dist_id),
        }

    # Priority 3: raw query fallback
    query = state.get("query", "").strip()
    if not query:
        return None
    detected_abbr, detected_fips = _extract_state_from_query(query)
    return {
        "state_fips":    detected_fips,
        "state_abbr":    detected_abbr,
        "district_type": "",
        "district_id":   "",
        "incumbent":     None,
        "office_type":   _detect_office_from_query(query),
        "district_label": query[:120],
    }


# ---------------------------------------------------------------------------
# Candidate matching, search helpers, fallback helpers, and memo formatting
# ---------------------------------------------------------------------------

def _find_matching_candidate(
    candidates_text: str,
    context: dict,
    target_office: str,
    is_exact: bool = True,
) -> Optional[tuple[str, bool]]:
    """
    Parse MCP search result text and return (candidate_name, is_exact).
    is_exact is passed by the caller — True for Steps 1-2, False for Steps 3-4.
    Returns None if no candidate is found or the LLM returns NONE.
    """
    from langchain_openai import ChatOpenAI
    llm = ChatOpenAI(
        model="gpt-4o-mini",
        temperature=0,
        openai_api_key=os.environ["OPENAI_API_KEY"],
    )
    state = context.get("state_abbr") or context.get("state_fips") or "unknown"
    prompt = f"""Below is a list of candidates from the American Bridge Research Books database.

Target race:
- State: {state}
- Office: {target_office}
- District: {context['district_label']}
- Known opponent name: {context['incumbent'] or 'unknown'}

**IMPORTANT: Only return a candidate if they represent exactly this district. If the list contains candidates from other districts in the same state but NOT this specific district, return NONE.**

Candidate list:
{candidates_text[:4000]}

Does this list contain a candidate matching the target race above?
If yes, reply with ONLY the candidate's full name, exactly as it appears in the list.
If no match, reply with ONLY the word NONE.
Do not explain. Do not add any other text."""
    try:
        result = llm.invoke(prompt).content.strip()
    except Exception as e:
        logger.warning(f"OppositionResearch: candidate-matching LLM call failed — {e}")
        return None

    first_line = result.splitlines()[0].strip() if result else ""
    if not first_line or first_line.upper() == "NONE":
        return None
    return first_line, is_exact


def _search_by_name(client: Any, provider_type: str, candidate_name: str) -> str:
    """Search Research Books by candidate name. Returns raw MCP response text."""
    return _query_mcp(
        client, provider_type,
        f"Use Research Books to find opposition research on {candidate_name}. "
        f"List all available pages and vulnerabilities.",
    )


def _build_office_search_query(state_abbr: str, state_name: str, office: str) -> list[tuple[str, str]]:
    state_lower = state_name.lower()
    if office == "U.S. Senate":
        return [
            ("page",   f"{state_lower}-senate"),
            ("search", f"{state_name} Senate Republican candidate"),
        ]
    if office == "Governor":
        return [
            ("page",   f"{state_abbr}-Gov"),
            ("page",   f"{state_lower}-state"),
            ("search", f"{state_name} Governor Republican candidate"),
        ]
    if office == "U.S. House":
        return [
            ("page",   f"{state_lower}-house"),
            ("search", f"{state_name} House Republican candidates"),
        ]
    return [("search", f"{state_name} {office}")]


def _search_by_office(
    client: Any, provider_type: str, state_abbr: str, state_name: str, office: str
) -> str:
    """Search Research Books for candidates by state and office using slug-aware queries."""
    for prompt_type, query in _build_office_search_query(state_abbr, state_name, office):
        if prompt_type == "page":
            prompt = f"Use Research Books to get the page at path '{query}' using rb_get_page and list all candidates and their research."
        else:
            prompt = f"Use Research Books to find {query}"
        result = _query_mcp(client, provider_type, prompt)
        if result and len(result) > 100:
            if prompt_type == "page" and not _extract_paths_from_content(result, state_abbr):
                continue
            return result
    return ""


_NON_CANDIDATE_PREFIXES = (
    "localimpact/", "home/", "vetting-reports/", "maga-files/", "en/",
)

def _extract_paths_from_content(content: str, state_abbr: str = "") -> list[str]:
    """
    Parse candidate page paths from MCP response text.
    Recognises markdown links, backtick paths, and bold numbered-list names.
    Returns a deduplicated slug list.
    """
    import re
    raw: list[str] = []

    # Markdown links: [text](/path) or [text](https://research-books.com/path)
    for href in re.findall(r'\]\(([^)]+)\)', content):
        href = href.strip()
        # Strip full URL prefix if present
        href = re.sub(r'^https?://[^/]+/', '', href)
        href = href.lstrip('/')
        raw.append(href)

    # Backtick paths: `some-slug`
    for bt in re.findall(r'`([^`]+)`', content):
        bt = bt.strip().lstrip('/')
        if re.match(r'^[a-zA-Z0-9][a-zA-Z0-9/_-]*$', bt):
            raw.append(bt)

    seen: set[str] = set()
    result: list[str] = []
    for path in raw:
        if not path or len(path) > 60:
            continue
        if any(path.startswith(prefix) for prefix in _NON_CANDIDATE_PREFIXES):
            continue
        # Must look like a slug (letters, digits, hyphens, slashes, underscores)
        if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9/_-]*$', path):
            continue
        if path not in seen:
            seen.add(path)
            result.append(path)

    # Fallback: numbered bold list like "1. **David Schweikert**" (no hyperlinks)
    if not result and state_abbr:
        prefix = f"{state_abbr}-Gov/"
        for name in re.findall(r'\d+\.\s+\*\*([^*]+)\*\*', content):
            slug = name.strip().lower().replace(' ', '-')
            path = f"{prefix}{slug}"
            if path not in seen and len(path) <= 60:
                seen.add(path)
                result.append(path)

    return result


def _clean_district_label(label: str, state_abbr: str) -> str:
    """
    Return a clean district label safe for use in memo headers and fallback notes.
    If label looks like a raw query string (too long or contains 3+ lowercase words)
    replace it with '{state_abbr} (statewide)'.
    """
    if len(label) > 60:
        return f"{state_abbr} (statewide)"
    lowercase_words = [w for w in label.split() if w.islower() and len(w) > 1]
    if len(lowercase_words) >= 3:
        return f"{state_abbr} (statewide)"
    return label


def _extract_candidate_names_from_content(content: str) -> list[str]:
    """
    Extract all primary candidate names from opposition research content.
    Returns a list of names; empty list if none can be determined.
    """
    from langchain_openai import ChatOpenAI
    llm = ChatOpenAI(
        model="gpt-4o-mini",
        temperature=0,
        openai_api_key=os.environ["OPENAI_API_KEY"],
    )
    prompt = f"""The following is opposition research content about one or more political candidates.
List the full names of ALL candidates who are the PRIMARY subjects of this research.
Do not include names mentioned only as references or context (e.g. Trump, Biden).
Focus on 2026 candidates only — ignore current officeholders who are not running in 2026.
Return one name per line. If you cannot determine any name, return NONE.

Content:
{content[:3000]}"""
    try:
        result = llm.invoke(prompt).content.strip()
    except Exception as e:
        logger.warning(f"OppositionResearch: name extraction LLM call failed — {e}")
        return []
    return [
        line.strip()
        for line in result.splitlines()
        if line.strip() and line.strip().upper() != "NONE" and len(line.strip()) <= 60
    ]


def _build_fallback_note(
    candidate_name: str,
    office_used: str,
    original_district_label: str,
    state_abbr: str,
    demographic_intent: str,
) -> str:
    """
    Build the italicised fallback disclaimer inserted at the top of the memo
    when opposition research is sourced from a different race than requested.

    Example:
        *Note: No Research Books entry found for VA-07 (U.S. House). Using
        Winsome Earle-Sears (Governor, VA) as a proxy for issue vulnerabilities
        relevant to youth voters in VA.*
    """
    clean_label = _clean_district_label(original_district_label, state_abbr)
    demo_part = (
        f" relevant to {demographic_intent} voters"
        if demographic_intent and demographic_intent != "default"
        else ""
    )
    return (
        f"*Note: No Research Books entry found for {clean_label}. "
        f"Using {candidate_name} ({office_used}, {state_abbr}) as a proxy for "
        f"issue vulnerabilities{demo_part} in {state_abbr}.*"
    )


def _format_memo(
    candidate_name: str,
    context: dict,
    research_text: str,
    demographic_intent: str,
    fallback_note: str | None = None,
) -> str:
    """
    Structure the raw Research Books content into the standard opposition
    research memo format using gpt-4o.  Falls back to raw content on failure.
    When fallback_note is provided it is inserted after the header line and
    before the ### Opponent Profile section.
    """
    from langchain_openai import ChatOpenAI
    llm = ChatOpenAI(
        model="gpt-4o",
        temperature=0,
        openai_api_key=os.environ["OPENAI_API_KEY"],
    )

    demographic_note = (
        f"The campaign is targeting {demographic_intent} voters. "
        f"Prioritize vulnerabilities and contrast angles most salient to this "
        f"demographic group in the Contrast Messaging section."
        if demographic_intent and demographic_intent != "default"
        else ""
    )

    multi = "," in candidate_name
    if multi:
        opening = (
            f"Convert the following Research Books content into a combined opposition research memo "
            f"covering these Republican candidates: {candidate_name}. "
            f"The race is in {context['district_label']}. "
            f"Cover all candidates in the memo sections below, grouping vulnerabilities and contrast angles by candidate where they differ."
        )
        profile_instruction = "[For each candidate: Name, party, office sought, district, notable background. Use sub-headers if multiple candidates.]"
    else:
        opening = (
            f"Convert the following Research Books content into a professional opposition research memo for {candidate_name} "
            f"running in {context['district_label']}."
        )
        profile_instruction = "[Name, party, office sought, district, notable background]"

    prompt = f"""
You are a Democratic opposition research analyst. {opening}

{demographic_note}

Research Books content:
{research_text[:12000]}

Write ONLY these four sections using these exact headers. Be specific and cite evidence.

### Opponent Profile
{profile_instruction}

### Key Vulnerabilities by Issue Area
[Bullet points: Issue Area → specific vulnerability with supporting evidence or vote]

### Contrast Messaging Angles by Demographic Group
[Bullet points: Demographic → recommended contrast message grounded in the research]
{f"Prioritize {demographic_intent} voters first." if demographic_intent and demographic_intent != "default" else ""}

### Suggested Attacks to Avoid
[Bullet points: approaches that lack evidence, may backfire, or energize the opponent's base]
"""
    try:
        body = llm.invoke(prompt).content.strip()
    except Exception as e:
        logger.warning(f"OppositionResearch: memo formatting LLM call failed — {e}")
        body = research_text[:6000]

    district_label = _clean_district_label(
        context["district_label"], context.get("state_abbr", "")
    )
    header = (
        f"--- MEMO FROM SOURCE: American Bridge Research Books "
        f"| CANDIDATE: {candidate_name} "
        f"| DISTRICT: {district_label} ---\n\n"
        f"*Opposition research sourced from American Bridge Research Books. "
        f"Covers Republican candidates in competitive 2026 House, Senate, and Governor races.*\n"
    )
    fallback_section = f"\n{fallback_note}\n" if fallback_note else ""
    return f"{header}{fallback_section}\n{body}"


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class OppositionResearchAgent:
    """
    Opposition Research agent — connects to the American Bridge Research Books
    MCP server and retrieves a Republican candidate research book using a
    five-step name-first cascading search.

    Steps:
      1. Name search  — _search_by_name() returns full research content directly;
                        no candidate-list matching needed.
      2. House sweep  — _search_by_office() returns candidate list; matched by
                        district via _find_matching_candidate(); full content
                        fetched with a second _search_by_name() call.
      3. Governor     — _search_by_office() content used directly; name extracted
                        via _find_matching_candidate() for the memo header.
      4. U.S. Senate  — same pattern as Step 3.
      5. Trump        — state-level impact content used directly as last resort.

    A fallback_note is inserted for Steps 3-5.  If all five steps return empty
    results the node returns silently with no memo or error.

    Input  (from AgentState):
        structured_data    — geographic context from election_results (preferred)
        query              — fallback when structured_data is not yet populated
        demographic_intent — from intent_router; used to prioritize contrast angles

    Output (to AgentState):
        research_results   — structured opposition research memo (if found)
        active_agents      — ["opposition_research"] (always)
    """

    @staticmethod
    def run(state: AgentState) -> dict:
        context = _extract_district_context(state)
        if context is None:
            logger.info("OppositionResearch: no district context available — skipping.")
            return {"active_agents": ["opposition_research"]}

        state_abbr         = context["state_abbr"]
        state_name         = _STATE_ABBR_TO_NAME.get(state_abbr, state_abbr)
        district_type      = context["district_type"]
        demographic_intent = state.get("demographic_intent") or "default"

        try:
            client, provider_type = _get_mcp_client()
        except RuntimeError as e:
            logger.warning(f"OppositionResearch: MCP client unavailable — {e}")
            return {"active_agents": ["opposition_research"]}

        candidate_name: str | None = None
        research_text:  str | None = None
        office_used:    str        = context["office_type"]
        fallback_note:  str | None = None

        try:
            # ------------------------------------------------------------------
            # Step 1 — Name search (incumbent known from election_results)
            # ------------------------------------------------------------------
            incumbent = context.get("incumbent")
            if incumbent:
                text_1 = _search_by_name(client, provider_type, incumbent)
                if text_1 and len(text_1) > 100:
                    candidate_name = incumbent
                    office_used    = context["office_type"]
                    research_text  = text_1
                    # fallback_note stays None — exact match, no disclaimer

            # ------------------------------------------------------------------
            # Step 2 — Congressional sweep (no incumbent name, House races only)
            # ------------------------------------------------------------------
            if candidate_name is None and district_type in ("house", "congressional"):
                text_2 = _search_by_office(client, provider_type, state_abbr, state_name,"U.S. House")
                if text_2:
                    match = _find_matching_candidate(
                        text_2, context, target_office=context["office_type"], is_exact=True
                    )
                    if match is not None:
                        matched_name   = match[0]
                        full_text      = _search_by_name(client, provider_type, matched_name)
                        if full_text and len(full_text) > 100:
                            candidate_name = matched_name
                            office_used    = context["office_type"]
                            research_text  = full_text
                            # fallback_note stays None — district-specific match

            # ------------------------------------------------------------------
            # Step 3 — Governor fallback
            # ------------------------------------------------------------------
            if candidate_name is None and district_type != "senate":
                text_3 = _search_by_office(client, provider_type, state_abbr, state_name, "Governor")
                if text_3 and len(text_3) > 100:
                    logger.info(f"OppositionResearch Step 3: text_3 len={len(text_3)}")
                    paths_3 = _extract_paths_from_content(text_3, state_abbr)
                    logger.info(f"OppositionResearch Step 3: paths_3={paths_3}")
                    if paths_3:
                        responses_3 = [
                            r for path in paths_3
                            if (r := _query_mcp(client, provider_type,
                                f"Use Research Books to get the page at path '{path}' using rb_get_page and provide all research content."))
                        ]
                        research_text_3 = "\n\n---\n\n".join(responses_3) if responses_3 else text_3
                        all_names_3: list[str] = []
                        for r in (responses_3 or [text_3]):
                            all_names_3.extend(_extract_candidate_names_from_content(r))
                    else:
                        research_text_3 = text_3
                        all_names_3 = _extract_candidate_names_from_content(text_3)
                    name_3         = ", ".join(all_names_3) if all_names_3 else f"{state_abbr} Republican Governor candidate"
                    candidate_name = name_3
                    office_used    = "Governor"
                    research_text  = research_text_3
                    fallback_note  = _build_fallback_note(
                        candidate_name, office_used,
                        context["district_label"], state_abbr, demographic_intent,
                    )

            # ------------------------------------------------------------------
            # Step 4 — U.S. Senate fallback
            # ------------------------------------------------------------------
            if candidate_name is None and district_type != "governor":
                text_4 = _search_by_office(client, provider_type, state_abbr, state_name, "U.S. Senate")
                if text_4 and len(text_4) > 100:
                    paths_4 = _extract_paths_from_content(text_4, state_abbr)
                    if paths_4:
                        responses_4 = [
                            r for path in paths_4
                            if (r := _query_mcp(client, provider_type,
                                f"Use Research Books to get the page at path '{path}' using rb_get_page and provide all research content."))
                        ]
                        research_text_4 = "\n\n---\n\n".join(responses_4) if responses_4 else text_4
                        all_names_4: list[str] = []
                        for r in (responses_4 or [text_4]):
                            all_names_4.extend(_extract_candidate_names_from_content(r))
                    else:
                        research_text_4 = text_4
                        all_names_4 = _extract_candidate_names_from_content(text_4)
                    if not all_names_4:
                        all_names_4 = _extract_candidate_names_from_content(text_4)
                    name_4         = ", ".join(all_names_4) if all_names_4 else f"{state_abbr} Republican Senate candidate"
                    candidate_name = name_4
                    office_used    = "U.S. Senate"
                    research_text  = research_text_4
                    fallback_note  = _build_fallback_note(
                        candidate_name, office_used,
                        context["district_label"], state_abbr, demographic_intent,
                    )

            # ------------------------------------------------------------------
            # Step 5 — Trump state-level impacts (always available as last resort)
            # ------------------------------------------------------------------
            if candidate_name is None:
                text_5 = _query_mcp(client, provider_type,
                    f"Use Research Books to find impacts in {state_name} "
                    f"including local economic impacts, federal funding cuts, "
                    f"and policy effects.",
                    timeout=60,
                )
                if text_5:
                    candidate_name = "Donald Trump"
                    office_used    = "President"
                    research_text  = text_5
                    fallback_note  = _build_fallback_note(
                        candidate_name, office_used,
                        context["district_label"], state_abbr, demographic_intent,
                    )

            # ------------------------------------------------------------------
            # All five steps exhausted — return silently with no memo
            # ------------------------------------------------------------------
            if candidate_name is None or not research_text:
                logger.info(
                    f"No Research Books entry found for {context['district_label']} "
                    "— skipping opposition research node"
                )
                return {"active_agents": ["opposition_research"]}

        except Exception as e:
            logger.warning(f"OppositionResearch: MCP query failed — {e}")
            return {"active_agents": ["opposition_research"]}

        memo = _format_memo(
            candidate_name, context, research_text, demographic_intent,
            fallback_note=fallback_note,
        )

        logger.info(
            f"OppositionResearch: retrieved research book for {candidate_name} "
            f"({office_used}, {context['district_label']})"
        )

        return {
            "research_results": [memo],
            "active_agents":    ["opposition_research"],
        }
