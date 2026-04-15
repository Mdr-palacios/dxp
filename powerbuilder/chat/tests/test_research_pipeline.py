# powerbuilder/chat/tests/test_research_pipeline.py
"""
Live integration tests for the research ingestion and retrieval pipeline.

Test sections:
  0. Pre-flight: API keys, packages, PDF existence
  1. ingestor.py -- upload The-Political-Disconnect-web2.pdf to __default__ namespace
  1a. Pinecone vector count before vs after upload
  2. researcher.py -- query 'what does the research say about young voters'
                      with org_namespace='general' (searches __default__ only)
  2a. Print result count, sources, dates
  2b. Recency sort verification
  3. researcher.py -- same query with fake org_namespace='test_org_123'
  3a. Confirm no crash, confirm only general results returned

Run from the project root:
  python -m chat.tests.test_research_pipeline
  -- or --
  python chat/tests/test_research_pipeline.py
"""

from dotenv import load_dotenv
load_dotenv()  # must be before any import that reads env vars

import logging
import os
import re
import sys
import time

# -- Path setup ---------------------------------------------------------------
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

# -- Logging ------------------------------------------------------------------
logging.basicConfig(
    level=logging.WARNING,     # suppress noisy SDK debug logs in test output
    format="  [LOG] %(message)s",
    stream=sys.stdout,
)
for noisy in ("httpx", "httpcore", "openai", "pinecone", "urllib3", "requests",
              "langchain", "llama_parse"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

# -- Test helpers -------------------------------------------------------------

_GREEN  = "\033[32m"
_RED    = "\033[31m"
_YELLOW = "\033[33m"
_RESET  = "\033[0m"

_results: list = []


def check(name: str, actual, expected, *, note: str = "") -> bool:
    passed = actual == expected
    tag = f"{_GREEN}PASS{_RESET}" if passed else f"{_RED}FAIL{_RESET}"
    _results.append((name, passed))
    print(f"  [{tag}] {name}")
    if not passed:
        print(f"         expected : {expected!r}")
        print(f"         actual   : {actual!r}")
    elif note:
        print(f"         {_YELLOW}{note}{_RESET}")
    return passed


def check_true(name: str, value: bool, *, note: str = "") -> bool:
    return check(name, bool(value), True, note=note)


def section(title: str):
    print(f"\n{'-' * 64}")
    print(f"  {title}")
    print("-" * 64)


def skip(name: str, reason: str):
    print(f"  [{_YELLOW}SKIP{_RESET}] {name}")
    print(f"         {reason}")
    _results.append((name, None))


def info(label: str, value):
    print(f"  [INFO] {label}: {value}")


def warn(msg: str):
    print(f"  [{_YELLOW}WARN{_RESET}] {msg}")


# -- Constants ----------------------------------------------------------------

QUERY          = "what does the research say about young voters"
GENERAL_NS     = "general"      # researcher.py skips org search when this is passed
PINECONE_NS    = "__default__"  # the Pinecone namespace researcher.py searches for general docs
FAKE_ORG_NS    = "test_org_123"
UPLOAD_NS      = "__default__"  # ingest the test PDF into the general namespace

PDF_PATH = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "../../research_memos/May 2025 Poll on Latinos, Trump and Immigration.pdf",
    )
)

PINECONE_STATS_SLEEP = 25  # seconds to wait after upsert before querying stats


# =============================================================================
# Helper: get Pinecone namespace vector count
# =============================================================================

def _get_namespace_vector_count(index, namespace: str) -> int:
    """
    Returns the current vector count for a given Pinecone namespace.

    langchain_pinecone maps the sentinel "__default__" to Pinecone's actual
    default namespace, which is represented as "" (empty string) in the
    describe_index_stats() response. We apply the same mapping here so the
    stats check stays consistent with what the vector store reads/writes.

    Returns 0 if the namespace has no vectors yet, -1 on error.
    """
    # Map langchain_pinecone's sentinel to Pinecone's internal default namespace key
    pinecone_ns = "" if namespace == "__default__" else namespace
    try:
        stats = index.describe_index_stats()
        ns_stats = stats.namespaces or {}
        if pinecone_ns in ns_stats:
            return ns_stats[pinecone_ns].vector_count
        return 0
    except Exception as e:
        warn(f"Could not read Pinecone stats: {e}")
        return -1


# =============================================================================
# Helper: parse date from a formatted memo string
# =============================================================================

def _parse_date_from_memo(memo: str):
    """
    Extracts the DATE field from a formatted memo string and parses it.
    Returns a datetime on success, None for 'date unknown' or unparseable values.
    """
    from chat.agents.researcher import _parse_date
    match = re.search(r"\|\s*DATE:\s*(.+?)\s*---", memo)
    if not match:
        return None
    raw = match.group(1).strip()
    if raw.lower() == "date unknown":
        return None
    return _parse_date(raw)


def _parse_source_from_memo(memo: str) -> str:
    """Extracts the SOURCE field from a formatted memo string."""
    match = re.search(r"MEMO FROM SOURCE:\s*(.+?)\s*\|", memo)
    return match.group(1).strip() if match else "unknown"


# =============================================================================
# 0. Pre-flight
# =============================================================================

section("0 - Pre-flight checks")

# Required packages
try:
    from pinecone import Pinecone as PineconeClient
    has_pinecone_sdk = True
    check_true("pinecone SDK is installed", True)
except ImportError:
    has_pinecone_sdk = False
    skip("pinecone SDK installed", "pip install pinecone-client")

try:
    import llama_parse  # noqa: F401
    has_llama = True
    check_true("llama_parse is installed", True)
except ImportError:
    has_llama = False
    skip("llama_parse installed", "pip install llama-parse -- required for ingestor")

try:
    from langchain_pinecone import PineconeVectorStore  # noqa: F401
    has_lc_pinecone = True
    check_true("langchain_pinecone is installed", True)
except ImportError:
    has_lc_pinecone = False
    skip("langchain_pinecone installed", "pip install langchain-pinecone")

# Required API keys
required_keys = {
    "OPENAI_API_KEY":           "OpenAI embeddings",
    "PINECONE_API_KEY":         "Pinecone read/write",
    "LLAMA_CLOUD_API_KEY":      "LlamaParse PDF parsing",
    "OPENAI_PINECONE_INDEX_NAME": "target Pinecone index name",
}
key_status = {}
for key, purpose in required_keys.items():
    present = bool(os.environ.get(key))
    key_status[key] = present
    if present:
        check_true(f"{key} is set ({purpose})", True)
    else:
        skip(f"{key} is set", f"Not set -- {purpose} will be unavailable")

has_all_keys = all(key_status.values())

# PDF source file
pdf_exists = os.path.isfile(PDF_PATH)
check_true("Test PDF exists", pdf_exists, note=PDF_PATH)
if not pdf_exists:
    warn(f"Expected: {PDF_PATH}")

# Pinecone index connection (needed for vector count checks)
pinecone_index = None
if has_pinecone_sdk and key_status.get("PINECONE_API_KEY") and key_status.get("OPENAI_PINECONE_INDEX_NAME"):
    try:
        _pc = PineconeClient(api_key=os.environ["PINECONE_API_KEY"])
        pinecone_index = _pc.Index(os.environ["OPENAI_PINECONE_INDEX_NAME"])
        check_true("Pinecone index connection established", True,
                   note=os.environ["OPENAI_PINECONE_INDEX_NAME"])
    except Exception as e:
        warn(f"Could not connect to Pinecone index: {e}")
        pinecone_index = None


# =============================================================================
# 1. ingestor.py -- Upload test PDF to __default__ namespace
# =============================================================================

section(f"1 - ingestor.py -- Upload PDF to Pinecone namespace '{UPLOAD_NS}'")

can_ingest = has_llama and has_lc_pinecone and has_all_keys and pdf_exists

if not can_ingest:
    missing = []
    if not has_llama:        missing.append("llama_parse")
    if not has_lc_pinecone: missing.append("langchain_pinecone")
    if not pdf_exists:       missing.append("PDF file")
    if not has_all_keys:     missing.append("API keys")
    skip("ingestor_node upload", f"Prerequisites missing: {', '.join(missing)}")
    count_before = count_after = -1
else:
    from chat.agents.ingestor import ingestor_node

    # -- 1a: Vector count before upload ---------------------------------------
    section("1a - Pinecone vector count before upload")

    count_before = _get_namespace_vector_count(pinecone_index, UPLOAD_NS) if pinecone_index else -1
    if count_before >= 0:
        info(f"Vectors in '{UPLOAD_NS}' before upload", count_before)
    else:
        info("Vector count check", "skipped (no Pinecone index connection)")

    # -- Run ingestor ---------------------------------------------------------
    print(f"\n  Parsing and uploading '{os.path.basename(PDF_PATH)}'...")
    print("  (LlamaParse API call -- this may take 30-60 seconds)")
    print()

    ingest_state = {
        "uploaded_file_path": PDF_PATH,
        "org_namespace":      UPLOAD_NS,
        "query":              "",
    }

    try:
        ingest_result = ingestor_node(ingest_state)
        ingest_ok = True
    except Exception as e:
        ingest_result = {"research_results": [f"Exception: {e}"]}
        ingest_ok = False

    ingest_msg = (ingest_result.get("research_results") or [""])[0]
    check_true(
        "ingestor_node returned without exception",
        ingest_ok,
        note=ingest_msg[:120] if ingest_ok else "",
    )
    check_true(
        "ingestor_node return message indicates success",
        "Successfully indexed" in ingest_msg,
        note=ingest_msg[:120],
    )

    # -- 1a continued: Vector count after upload ------------------------------
    section("1a (cont.) - Pinecone vector count after upload")

    if pinecone_index and count_before >= 0:
        print(f"  Waiting {PINECONE_STATS_SLEEP}s for Pinecone index stats to refresh...")
        time.sleep(PINECONE_STATS_SLEEP)
        count_after = _get_namespace_vector_count(pinecone_index, UPLOAD_NS)
        info(f"Vectors in '{UPLOAD_NS}' after upload", count_after)

        if count_after > count_before:
            check_true(
                f"Vector count increased after upload ({count_before} -> {count_after})",
                True,
                note=f"+{count_after - count_before} new vectors",
            )
        else:
            # Not necessarily a failure -- PDF may already have been indexed in a previous run
            _results.append(("Vector count increased after upload", None))
            print(
                f"  [{_YELLOW}INFO{_RESET}] Vector count did not increase "
                f"({count_before} -> {count_after}). "
                "PDF may already be indexed from a prior test run."
            )
    else:
        skip("Vector count after upload", "No Pinecone index connection")
        count_after = -1


# =============================================================================
# 2. researcher.py -- Query with general namespace
# =============================================================================

section(f"2 - researcher.py -- Query with org_namespace='{GENERAL_NS}'")

can_research = has_lc_pinecone and key_status.get("OPENAI_API_KEY") and key_status.get("PINECONE_API_KEY") and key_status.get("OPENAI_PINECONE_INDEX_NAME")

if not can_research:
    skip("research_node (general)", "langchain_pinecone or API keys missing")
else:
    from chat.agents.researcher import research_node

    print(f"  Query: {QUERY!r}")
    print(f"  org_namespace: {GENERAL_NS!r}  ->  searches Pinecone namespace '{PINECONE_NS}' only")
    print()

    research_state = {
        "query":         QUERY,
        "org_namespace": GENERAL_NS,
    }

    try:
        research_result = research_node(research_state)
        research_ok = True
    except Exception as e:
        research_result = {"research_results": [], "active_agents": []}
        research_ok = False
        warn(f"research_node raised: {e}")

    check_true("research_node returned without exception", research_ok)

    findings = research_result.get("research_results", [])
    check_true(
        "research_node returned a list",
        isinstance(findings, list),
    )
    check_true(
        "At least one result returned",
        len(findings) > 0,
        note=f"{len(findings)} chunks returned",
    )

    # -- 2a: Print result count, sources, dates -------------------------------
    section("2a - Result count, sources, and dates")

    info("Total chunks returned", len(findings))
    print()

    dated_docs   = []
    undated_docs = []
    for i, memo in enumerate(findings, 1):
        source   = _parse_source_from_memo(memo)
        date_dt  = _parse_date_from_memo(memo)
        date_raw = re.search(r"\|\s*DATE:\s*(.+?)\s*---", memo)
        date_str = date_raw.group(1).strip() if date_raw else "date unknown"
        print(f"  {i:>2}. source={source!r:<50}  date={date_str!r}")
        if date_dt:
            dated_docs.append((i, date_dt, source))
        else:
            undated_docs.append((i, source))

    print()
    info("Chunks with parseable date", len(dated_docs))
    info("Chunks with 'date unknown'", len(undated_docs))

    # -- 2b: Recency sort verification ----------------------------------------
    section("2b - Recency sort verification")

    # Rule 1: All dated chunks must come before all undated chunks
    if dated_docs and undated_docs:
        last_dated_idx  = dated_docs[-1][0]
        first_undated_idx = undated_docs[0][0]
        check_true(
            "All dated chunks appear before undated chunks",
            last_dated_idx < first_undated_idx,
            note=f"Last dated at position {last_dated_idx}, "
                 f"first undated at position {first_undated_idx}",
        )
    elif not dated_docs:
        _results.append(("All dated chunks appear before undated chunks", None))
        print(
            f"  [{_YELLOW}INFO{_RESET}] All returned chunks have 'date unknown' -- "
            "recency sort order cannot be verified (no date metadata in index). "
            "Add 'date' fields when ingesting documents to enable recency ranking."
        )
    else:
        _results.append(("All dated chunks appear before undated chunks", None))
        print(
            f"  [{_YELLOW}INFO{_RESET}] All returned chunks have a parseable date -- "
            "no undated docs to compare against."
        )

    # Rule 2: Dated chunks must be sorted descending (most recent first)
    if len(dated_docs) >= 2:
        dates_in_order = [dt for _, dt, _ in dated_docs]
        sorted_desc = all(
            dates_in_order[i] >= dates_in_order[i + 1]
            for i in range(len(dates_in_order) - 1)
        )
        check_true(
            "Dated chunks are sorted most-recent first",
            sorted_desc,
            note=f"Dates: {[dt.strftime('%Y-%m-%d') for dt in dates_in_order]}",
        )
    elif len(dated_docs) == 1:
        _results.append(("Dated chunks are sorted most-recent first", None))
        print(
            f"  [{_YELLOW}INFO{_RESET}] Only one dated chunk returned -- "
            "sort order cannot be verified with a single element."
        )
    else:
        _results.append(("Dated chunks are sorted most-recent first", None))
        print(
            f"  [{_YELLOW}INFO{_RESET}] No dated chunks returned -- "
            "sort order not verifiable."
        )


# =============================================================================
# 3. researcher.py -- Same query with fake org_namespace
# =============================================================================

section(f"3 - researcher.py -- Same query with fake org_namespace='{FAKE_ORG_NS}'")

if not can_research:
    skip("research_node (fake org)", "langchain_pinecone or API keys missing")
else:
    print(f"  Query: {QUERY!r}")
    print(f"  org_namespace: {FAKE_ORG_NS!r}  ->  namespace does not exist in Pinecone")
    print()

    fake_state = {
        "query":         QUERY,
        "org_namespace": FAKE_ORG_NS,
    }

    try:
        fake_result = fake_node_result = research_node(fake_state)
        fake_ok = True
    except Exception as e:
        fake_result = {"research_results": [], "active_agents": []}
        fake_ok = False
        warn(f"research_node raised on fake org: {e}")

    check_true(
        f"research_node does not crash with non-existent namespace '{FAKE_ORG_NS}'",
        fake_ok,
    )

    fake_findings = fake_result.get("research_results", [])
    check_true(
        "Returns a list (not None or exception) for fake org",
        isinstance(fake_findings, list),
    )

    # Should still return general results from __default__
    check_true(
        "Returns at least one result from __default__ (general) namespace",
        len(fake_findings) > 0,
        note=f"{len(fake_findings)} chunks returned",
    )

    # Content should come from __default__ only (same as general query)
    # Verify by checking sources -- they should match the general query sources
    if fake_findings and findings:
        fake_sources  = {_parse_source_from_memo(m) for m in fake_findings}
        gen_sources   = {_parse_source_from_memo(m) for m in findings}
        overlap       = fake_sources & gen_sources
        check_true(
            "Fake-org results share sources with general results (confirming __default__ is searched)",
            len(overlap) > 0,
            note=f"Shared sources: {sorted(overlap)}",
        )

    info("Chunks returned for fake org", len(fake_findings))
    print()
    for i, memo in enumerate(fake_findings, 1):
        source   = _parse_source_from_memo(memo)
        date_raw = re.search(r"\|\s*DATE:\s*(.+?)\s*---", memo)
        date_str = date_raw.group(1).strip() if date_raw else "date unknown"
        print(f"  {i:>2}. source={source!r:<50}  date={date_str!r}")


# =============================================================================
# Summary
# =============================================================================

section("Summary")

passed  = sum(1 for _, ok in _results if ok is True)
failed  = sum(1 for _, ok in _results if ok is False)
skipped = sum(1 for _, ok in _results if ok is None)
total   = len(_results)

print(f"\n  Total : {total}")
print(f"  {_GREEN}Passed{_RESET}: {passed}")
if failed:
    print(f"  {_RED}Failed{_RESET}: {failed}")
if skipped:
    print(f"  {_YELLOW}Skipped{_RESET}: {skipped}")
print()

if failed:
    print(f"  {_RED}FAILED TESTS:{_RESET}")
    for name, ok in _results:
        if ok is False:
            print(f"    x {name}")
    sys.exit(1)
else:
    print(
        f"  {_GREEN}All checks passed.{_RESET}" if not skipped
        else f"  {_GREEN}All non-skipped checks passed.{_RESET}"
    )
    sys.exit(0)
