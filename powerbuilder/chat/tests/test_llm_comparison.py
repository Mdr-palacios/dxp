"""
test_llm_comparison.py

Side-by-side LLM provider comparison test.

Two comparison modes are run for each configured provider:

  RAG Retrieval Comparison
  ------------------------
  For each provider:
    1. Retrieval  — searches the provider's Pinecone index using its native
                    embedding model (falls back to OpenAI for providers without
                    native embeddings: anthropic, groq).
    2. Completion — generates an answer using that provider's LLM against the
                    retrieved context (RAG prompt, no tool calls).
    3. Timing     — records wall-clock seconds for retrieval and completion separately.

  Full Pipeline Comparison
  ------------------------
  For each provider:
    Sets LLM_PROVIDER to that provider then calls run_query() from manager.py,
    which drives the full LangGraph pipeline (researcher → election_results →
    win_number → precincts → messaging → cost_calculator → synthesizer).
    Records final_answer, active_agents, errors, and total wall-clock time.
    This includes live API calls: Census CVAP, MEDSL election results, FEC data.

Providers are tested concurrently (one thread per provider).
Results are saved to exports/llm_comparison_report.md.

Run from the project root:
    python chat/tests/test_llm_comparison.py
    python -m pytest chat/tests/test_llm_comparison.py -v -s
"""

import json
import os
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

# load_dotenv MUST be first — before any import that touches API keys
from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import pytest

from chat.utils.llm_config import (
    PINECONE_INDEX_NAMES,
    EmbeddingConfig,
    get_completion_client,
    get_configured_providers,
    get_embedding_client,
)

# ---------------------------------------------------------------------------
# Test queries — same as test_full_pipeline.py
# ---------------------------------------------------------------------------

QUERIES = [
    {
        "id":    "young_voters",
        "label": "Q1: Young voter targeting + messaging",
        "text":  (
            "I want to reach young voters in Virginia's 7th Congressional District. "
            "What precincts should I target and what message should I deliver?"
        ),
    },
    {
        "id":    "canvassing_cost",
        "label": "Q2: Canvassing cost estimate",
        "text":  (
            "How much would it cost to run a canvassing program "
            "in Virginia's 7th Congressional District in 2026?"
        ),
    },
    {
        "id":    "win_number",
        "label": "Q3: Win number",
        "text":  "What is the win number for Virginia's 7th Congressional District in 2026?",
    },
]

# Pinecone top-K for RAG retrieval comparison
RETRIEVAL_K = 5

# Exports directory — mirrors export_node
EXPORTS_DIR = os.getenv(
    "EXPORTS_DIR",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "../../exports")),
)

# Non-fatal error fragments for the full pipeline (same list as test_full_pipeline.py)
NON_FATAL_PATTERNS = [
    "PrecinctsAgent: Census API",
    "Census API failure",
    "MEDSL",
    "FEC",
    "election_results",
    "No election data",
    "crosswalk",
]

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _index_exists(index_name: str) -> bool:
    """Return True if the named Pinecone index exists."""
    try:
        from pinecone import Pinecone
        pc = Pinecone(api_key=os.getenv("PINECONE_API_KEY", ""))
        return any(idx.name == index_name for idx in pc.list_indexes())
    except Exception:
        return False


def _fmt_time(val) -> str:
    return f"{val:.2f}s" if val is not None else "N/A"


_SKIP_PHRASES = ("does not exist", "API_KEY is required", "decommissioned", "was removed")

PIPELINE_CACHE_PATH = os.path.join(EXPORTS_DIR, "pipeline_results_cache.json")


# ---------------------------------------------------------------------------
# Pipeline result cache helpers
# ---------------------------------------------------------------------------

def save_pipeline_cache(results: list[dict]) -> None:
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    with open(PIPELINE_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"Pipeline results cached to: {PIPELINE_CACHE_PATH}")


def load_pipeline_cache() -> list[dict]:
    if not os.path.exists(PIPELINE_CACHE_PATH):
        raise FileNotFoundError(
            f"Cache not found at {PIPELINE_CACHE_PATH}. "
            "Run without --use-cached-results first to generate it."
        )
    with open(PIPELINE_CACHE_PATH, encoding="utf-8") as f:
        results = json.load(f)
    print(f"Loaded {len(results)} cached pipeline results from: {PIPELINE_CACHE_PATH}")
    return results


# ---------------------------------------------------------------------------
# RAG Retrieval Comparison — researcher + single LLM call
# ---------------------------------------------------------------------------

def _run_rag_one(provider_info: dict, query: dict) -> dict:
    """
    Execute one retrieval + single-prompt completion for *provider_info* against *query*.
    This is RAG-only: no tool calls, no multi-agent pipeline.
    """
    provider   = provider_info["provider"]
    query_id   = query["id"]
    query_text = query["text"]

    result = {
        "provider":           provider,
        "model":              provider_info["model"],
        "embedding_model":    provider_info.get("embedding_model") or "openai (fallback)",
        "index_name":         provider_info["index_name"],
        "query_id":           query_id,
        "query_label":        query["label"],
        "retrieval_sources":  [],
        "retrieval_snippets": [],
        "retrieval_time_s":   None,
        "completion_answer":  "",
        "completion_time_s":  None,
        "error":              None,
    }

    # 1. Retrieval
    try:
        emb_cfg    = get_embedding_client(provider=provider)
        index_name = emb_cfg.index_name

        if not _index_exists(index_name):
            result["error"] = (
                f"Index '{index_name}' does not exist. "
                "Run comparison_ingestor.py first."
            )
            return result

        from langchain_pinecone import PineconeVectorStore
        t0    = time.perf_counter()
        store = PineconeVectorStore(
            index_name=index_name,
            embedding=emb_cfg.client,
            namespace="__default__",
            text_key="text",
        )
        docs  = store.similarity_search(query_text, k=RETRIEVAL_K)
        result["retrieval_time_s"] = round(time.perf_counter() - t0, 2)

        result["retrieval_sources"] = [
            d.metadata.get("source", "unknown") for d in docs
        ]
        result["retrieval_snippets"] = [
            d.page_content[:200].replace("\n", " ") for d in docs
        ]
        context = "\n\n".join(
            f"[{d.metadata.get('source','?')} | {d.metadata.get('date','?')}]\n"
            f"{d.page_content[:800]}"
            for d in docs
        )
    except Exception as e:
        result["error"] = f"Retrieval failed: {e}"
        return result

    # 2. Completion
    try:
        llm    = get_completion_client(temperature=0.3, provider=provider)
        prompt = (
            "You are a political strategist. Answer the following question using "
            "only the research context provided. Be concise (3–5 sentences).\n\n"
            f"CONTEXT:\n{context}\n\n"
            f"QUESTION: {query_text}"
        )
        t0     = time.perf_counter()
        answer = llm.invoke(prompt).content.strip()
        result["completion_time_s"] = round(time.perf_counter() - t0, 2)
        result["completion_answer"] = answer
    except Exception as e:
        result["error"] = f"Completion failed: {e}"

    return result


def run_rag_comparison(providers: list[dict] | None = None) -> list[dict]:
    """
    Run RAG-only comparison for all configured providers against all QUERIES concurrently.
    """
    if providers is None:
        providers = get_configured_providers()

    if not providers:
        print("No providers configured. Check your API keys.")
        return []

    tasks = [(p, q) for p in providers for q in QUERIES]
    results: list[dict] = []

    print(f"\n[RAG] Running {len(providers)} providers x {len(QUERIES)} queries "
          f"= {len(tasks)} tasks (concurrent)\n")

    with ThreadPoolExecutor(max_workers=min(len(tasks), 8)) as pool:
        futures = {pool.submit(_run_rag_one, p, q): (p["provider"], q["id"]) for p, q in tasks}
        for future in as_completed(futures):
            provider_name, query_id = futures[future]
            try:
                res = future.result()
            except Exception as e:
                res = {
                    "provider":           provider_name,
                    "query_id":           query_id,
                    "error":              str(e),
                    "completion_answer":  "",
                    "retrieval_sources":  [],
                }
            with _lock:
                results.append(res)
                status = res.get("error") or "ok"
                print(f"  [RAG][{provider_name:12}] {query_id:20} -> {status}")

    return results


# ---------------------------------------------------------------------------
# Full Pipeline Comparison — run_query() through manager.py
# ---------------------------------------------------------------------------

def _run_pipeline_one(provider_info: dict, query: dict) -> dict:
    """
    Run the full LangGraph pipeline for *provider_info* against *query* by
    temporarily setting LLM_PROVIDER to the target provider and calling run_query().

    Records final_answer, active_agents, errors, and total wall-clock time.
    """
    provider   = provider_info["provider"]
    query_id   = query["id"]
    query_text = query["text"]

    result = {
        "provider":        provider,
        "model":           provider_info["model"],
        "query_id":        query_id,
        "query_label":     query["label"],
        "final_answer":    "",
        "active_agents":   [],
        "pipeline_errors": [],
        "pipeline_time_s": None,
        "error":           None,
    }

    # Set LLM_PROVIDER in the environment so manager.py and all agents pick it up.
    # Each thread gets its own invocation; os.environ mutation is process-wide so
    # we serialise pipeline runs with the lock to avoid cross-provider interference.
    try:
        with _lock:
            os.environ["LLM_PROVIDER"] = provider

        # Import here so manager_app picks up the updated env at invoke time.
        # The graph is compiled at module level in manager.py using get_model(),
        # which reads LLM_PROVIDER at call time — so this works correctly.
        from chat.agents.manager import run_query

        t0     = time.perf_counter()
        state  = run_query(query=query_text, org_namespace="general")
        result["pipeline_time_s"] = round(time.perf_counter() - t0, 2)

        result["final_answer"]    = state.get("final_answer", "")
        result["active_agents"]   = state.get("active_agents", [])
        result["pipeline_errors"] = state.get("errors", [])

        # Flag fatal errors (non-fatal patterns like Census/FEC API failures are expected)
        fatal_errors = [
            e for e in result["pipeline_errors"]
            if not any(pat in e for pat in NON_FATAL_PATTERNS)
        ]
        if fatal_errors:
            result["error"] = f"Pipeline errors: {'; '.join(fatal_errors[:3])}"

    except Exception as e:
        result["error"] = f"Pipeline failed: {e}"
    finally:
        # Restore to openai so other threads aren't affected after this run
        with _lock:
            os.environ["LLM_PROVIDER"] = "openai"

    return result


def run_pipeline_comparison(providers: list[dict] | None = None) -> list[dict]:
    """
    Run the full manager.py pipeline for each provider against all QUERIES.
    Runs sequentially per provider to avoid LLM_PROVIDER env conflicts.
    """
    if providers is None:
        providers = get_configured_providers()

    if not providers:
        print("No providers configured. Check your API keys.")
        return []

    results: list[dict] = []

    print(f"\n[Pipeline] Running {len(providers)} providers x {len(QUERIES)} queries "
          f"= {len(providers) * len(QUERIES)} tasks (sequential per provider)\n")

    # Sequential to prevent LLM_PROVIDER env var from being stepped on across threads
    for p in providers:
        for q in QUERIES:
            res = _run_pipeline_one(p, q)
            results.append(res)
            status = res.get("error") or f"{len(res.get('active_agents', []))} agents"
            print(f"  [Pipeline][{p['provider']:12}] {q['id']:20} -> {status}")

    return results


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------

def write_report(
    rag_results:      list[dict],
    pipeline_results: list[dict],
    path:             str,
) -> None:
    """Write a Markdown report with two sections: RAG Retrieval and Full Pipeline."""
    os.makedirs(os.path.dirname(path), exist_ok=True)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    rag_providers      = sorted({r["provider"] for r in rag_results})
    pipeline_providers = sorted({r["provider"] for r in pipeline_results})
    all_providers      = sorted(set(rag_providers) | set(pipeline_providers))

    lines = [
        "# Powerbuilder LLM Provider Comparison Report",
        "",
        f"**Generated:** {now}  ",
        f"**Providers tested:** {', '.join(all_providers)}  ",
        f"**Queries:** {len(QUERIES)}  ",
        "",
        "---",
        "",
    ]

    # -----------------------------------------------------------------------
    # Section 1: RAG Retrieval Comparison
    # -----------------------------------------------------------------------
    lines += [
        "# RAG Retrieval Comparison",
        "",
        "Each provider retrieves context from its own Pinecone index using its native "
        "embedding model (anthropic and groq fall back to OpenAI embeddings), then "
        "answers via a single prompt — no tool calls or multi-agent pipeline.",
        "",
        "---",
        "",
    ]

    rag_by_provider: dict[str, dict] = {}
    for r in rag_results:
        rag_by_provider.setdefault(r["provider"], {})[r["query_id"]] = r

    for provider in sorted(rag_by_provider.keys()):
        queries = rag_by_provider[provider]
        first   = next(iter(queries.values()))
        model   = first.get("model", "unknown")
        emb     = first.get("embedding_model", "unknown")
        idx     = first.get("index_name", "unknown")

        lines += [
            f"## {provider.title()}",
            "",
            "| Field | Value |",
            "|-------|-------|",
            f"| Completion model | `{model}` |",
            f"| Embedding model  | `{emb}` |",
            f"| Pinecone index   | `{idx}` |",
            "",
        ]

        for q in QUERIES:
            qid = q["id"]
            r   = queries.get(qid, {})
            err = r.get("error")

            lines += [
                f"### {q['label']}",
                "",
                f"> *{q['text']}*",
                "",
            ]

            if err:
                lines += [f"**Error:** `{err}`", ""]
                continue

            lines += [
                f"**Retrieval** ({_fmt_time(r.get('retrieval_time_s'))})  ",
                f"Sources: {', '.join(r.get('retrieval_sources', [])) or 'none'}",
                "",
            ]
            for i, snippet in enumerate(r.get("retrieval_snippets", []), 1):
                lines.append(f"{i}. _{snippet}_")
            lines.append("")

            lines += [
                f"**Completion** ({_fmt_time(r.get('completion_time_s'))})  ",
                "",
                r.get("completion_answer", "*(no answer)*"),
                "",
            ]

        lines += ["---", ""]

    # RAG timing summary
    lines += [
        "## RAG Timing Summary",
        "",
        "| Provider | Query | Retrieval | Completion | Total |",
        "|----------|-------|-----------|------------|-------|",
    ]
    for r in sorted(rag_results, key=lambda x: (x.get("provider",""), x.get("query_id",""))):
        if r.get("error"):
            continue
        ret  = r.get("retrieval_time_s")
        comp = r.get("completion_time_s")
        tot  = (ret or 0) + (comp or 0)
        lines.append(
            f"| {r['provider']:12} | {r['query_id']:20} | "
            f"{_fmt_time(ret):8} | {_fmt_time(comp):10} | {tot:.2f}s |"
        )
    lines += ["", "---", ""]

    # -----------------------------------------------------------------------
    # Section 2: Full Pipeline Comparison
    # -----------------------------------------------------------------------
    lines += [
        "# Full Pipeline Comparison",
        "",
        "Each provider runs the complete LangGraph pipeline via `run_query()`: "
        "researcher → election_results → win_number → precincts → messaging → "
        "cost_calculator → synthesizer. Includes live API calls (Census CVAP, "
        "MEDSL election results, FEC data). Census/FEC/MEDSL failures are non-fatal.",
        "",
        "---",
        "",
    ]

    pipeline_by_provider: dict[str, dict] = {}
    for r in pipeline_results:
        pipeline_by_provider.setdefault(r["provider"], {})[r["query_id"]] = r

    for provider in sorted(pipeline_by_provider.keys()):
        queries = pipeline_by_provider[provider]
        first   = next(iter(queries.values()))
        model   = first.get("model", "unknown")

        lines += [
            f"## {provider.title()}",
            "",
            "| Field | Value |",
            "|-------|-------|",
            f"| Completion model | `{model}` |",
            "",
        ]

        for q in QUERIES:
            qid = q["id"]
            r   = queries.get(qid, {})
            err = r.get("error")

            lines += [
                f"### {q['label']}",
                "",
                f"> *{q['text']}*",
                "",
            ]

            if err:
                lines += [f"**Error:** `{err}`", ""]
                continue

            agents = ", ".join(r.get("active_agents", [])) or "none"
            errors = r.get("pipeline_errors", [])

            lines += [
                f"**Pipeline time:** {_fmt_time(r.get('pipeline_time_s'))}  ",
                f"**Agents called:** {agents}  ",
            ]
            if errors:
                lines.append(
                    f"**Non-fatal errors ({len(errors)}):** "
                    + "; ".join(errors[:3])
                    + ("..." if len(errors) > 3 else "")
                )
            lines += [
                "",
                "**Final Answer:**",
                "",
                r.get("final_answer", "*(no answer)*"),
                "",
            ]

        lines += ["---", ""]

    # Pipeline timing summary
    lines += [
        "## Full Pipeline Timing Summary",
        "",
        "| Provider | Query | Total Time | Agents |",
        "|----------|-------|------------|--------|",
    ]
    for r in sorted(pipeline_results, key=lambda x: (x.get("provider",""), x.get("query_id",""))):
        if r.get("error"):
            continue
        agents = len(r.get("active_agents", []))
        lines.append(
            f"| {r['provider']:12} | {r['query_id']:20} | "
            f"{_fmt_time(r.get('pipeline_time_s')):10} | {agents} |"
        )
    lines += ["", "---", ""]

    # ChangeAgent placeholder
    lines += [
        "## ChangeAgent",
        "",
        "ChangeAgent: pending API integration",
        "",
        "_This section will be populated automatically once ChangeAgent is registered "
        "via `register_custom_provider()` in llm_config.py._",
        "",
        "---",
        "",
    ]

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"\nReport saved to: {path}")


# ---------------------------------------------------------------------------
# pytest fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def rag_results():
    """Run RAG comparison once per test module and cache results."""
    return run_rag_comparison()


@pytest.fixture(scope="module")
def pipeline_results():
    """
    Run full pipeline comparison once per test module and cache results.
    Set USE_CACHED_RESULTS=1 to load from exports/pipeline_results_cache.json
    instead of re-executing the pipeline (equivalent to --use-cached-results
    in the standalone runner).
    """
    if os.getenv("USE_CACHED_RESULTS", "").lower() in ("1", "true", "yes"):
        return load_pipeline_cache()
    results = run_pipeline_comparison()
    save_pipeline_cache(results)
    return results


# Keep the old fixture name so any external callers aren't broken
@pytest.fixture(scope="module")
def comparison_results(rag_results):
    return rag_results


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------

class TestSection0PreFlight:

    def test_openai_key_present(self):
        assert os.getenv("OPENAI_API_KEY"), "OPENAI_API_KEY is required (used as embedding fallback)"

    def test_pinecone_key_present(self):
        assert os.getenv("PINECONE_API_KEY"), "PINECONE_API_KEY is required"

    def test_at_least_one_provider_configured(self):
        providers = get_configured_providers()
        assert providers, "No LLM providers configured — set at least one API key"
        print(f"\nConfigured providers: {[p['provider'] for p in providers]}")

    def test_openai_index_exists(self):
        src = os.getenv("OPENAI_PINECONE_INDEX_NAME", "openai-research-index")
        assert _index_exists(src), (
            f"Source index '{src}' not found. Run bulk_upload.py first."
        )


class TestSection1RagResults:

    def test_all_providers_returned_answers(self, rag_results):
        """Every configured provider should produce a non-empty RAG answer for Q3."""
        providers = get_configured_providers()
        for p in providers:
            name = p["provider"]
            r = next(
                (x for x in rag_results
                 if x["provider"] == name and x["query_id"] == "win_number"),
                None,
            )
            assert r is not None, f"No RAG result for provider '{name}'"
            if r.get("error") and any(phrase in r["error"] for phrase in _SKIP_PHRASES):
                pytest.skip(f"{name} skipped: {r['error']}")
            assert not r.get("error"), f"{name} RAG error: {r['error']}"
            assert len(r.get("completion_answer", "")) > 20, \
                f"{name} returned an empty RAG answer"

    def test_retrieval_times_recorded(self, rag_results):
        """Retrieval time must be recorded for every successful RAG result."""
        for r in rag_results:
            if r.get("error"):
                continue
            assert r.get("retrieval_time_s") is not None, \
                f"Missing retrieval_time_s for {r['provider']} / {r['query_id']}"


class TestSection2PipelineResults:

    def test_all_providers_completed_pipeline(self, pipeline_results):
        """Every configured provider should complete the full pipeline for Q3."""
        providers = get_configured_providers()
        for p in providers:
            name = p["provider"]
            r = next(
                (x for x in pipeline_results
                 if x["provider"] == name and x["query_id"] == "win_number"),
                None,
            )
            assert r is not None, f"No pipeline result for provider '{name}'"
            if r.get("error") and any(phrase in r["error"] for phrase in _SKIP_PHRASES):
                pytest.skip(f"{name} skipped: {r['error']}")
            assert not r.get("error"), f"{name} pipeline error: {r['error']}"
            assert len(r.get("final_answer", "")) > 20, \
                f"{name} pipeline returned an empty final answer"

    def test_pipeline_agents_were_called(self, pipeline_results):
        """researcher must appear for young_voters queries (which always need RAG context)."""
        for r in pipeline_results:
            if r.get("error") or r.get("query_id") != "young_voters":
                continue
            assert "researcher" in r.get("active_agents", []), (
                f"{r['provider']} / {r['query_id']}: researcher not in active_agents "
                f"({r.get('active_agents')})"
            )

    def test_pipeline_times_recorded(self, pipeline_results):
        """Pipeline time must be recorded for every successful result."""
        for r in pipeline_results:
            if r.get("error"):
                continue
            assert r.get("pipeline_time_s") is not None, \
                f"Missing pipeline_time_s for {r['provider']} / {r['query_id']}"


class TestSection3Report:

    def test_report_written(self, rag_results, pipeline_results):
        """Report file must be created and non-empty."""
        path = os.path.join(EXPORTS_DIR, "llm_comparison_report.md")
        write_report(rag_results, pipeline_results, path)
        assert os.path.exists(path)
        assert os.path.getsize(path) > 500
        print(f"\nReport: {path}")

    def test_report_contains_both_sections(self, rag_results, pipeline_results):
        """Report must contain both comparison section headings."""
        path = os.path.join(EXPORTS_DIR, "llm_comparison_report.md")
        if not os.path.exists(path):
            write_report(rag_results, pipeline_results, path)
        content = open(path, encoding="utf-8").read()
        assert "# RAG Retrieval Comparison" in content
        assert "# Full Pipeline Comparison" in content

    def test_report_contains_change_agent_placeholder(self, rag_results, pipeline_results):
        """The ChangeAgent placeholder must appear in the report."""
        path = os.path.join(EXPORTS_DIR, "llm_comparison_report.md")
        if not os.path.exists(path):
            write_report(rag_results, pipeline_results, path)
        content = open(path, encoding="utf-8").read()
        assert "ChangeAgent: pending API integration" in content


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Run LLM provider comparison.")
    ap.add_argument(
        "--use-cached-results",
        action="store_true",
        help=(
            f"Load pipeline results from {PIPELINE_CACHE_PATH} instead of "
            "re-running the full pipeline. For pytest, set USE_CACHED_RESULTS=1."
        ),
    )
    args = ap.parse_args()

    providers = get_configured_providers()
    print(f"Configured providers ({len(providers)}):")
    for p in providers:
        emb = "native embed" if p["embedding_available"] else "OpenAI embed fallback"
        print(f"  {p['provider']:12} completion={p['model']}  [{emb}]")

    rag_res = run_rag_comparison(providers)

    if args.use_cached_results:
        pipeline_res = load_pipeline_cache()
    else:
        pipeline_res = run_pipeline_comparison(providers)
        save_pipeline_cache(pipeline_res)

    path = os.path.join(EXPORTS_DIR, "llm_comparison_report.md")
    write_report(rag_res, pipeline_res, path)

    print("\n=== RAG ANSWER LENGTHS ===")
    for r in sorted(rag_res, key=lambda x: (x.get("provider",""), x.get("query_id",""))):
        if r.get("error"):
            print(f"  {r['provider']:12} {r['query_id']:20}  ERROR: {r['error'][:60]}")
        else:
            print(
                f"  {r['provider']:12} {r['query_id']:20}  "
                f"ret={_fmt_time(r.get('retrieval_time_s')):6}  "
                f"cmp={_fmt_time(r.get('completion_time_s')):6}  "
                f"ans={len(r.get('completion_answer',''))} chars"
            )

    print("\n=== PIPELINE ANSWER LENGTHS ===")
    for r in sorted(pipeline_res, key=lambda x: (x.get("provider",""), x.get("query_id",""))):
        if r.get("error"):
            print(f"  {r['provider']:12} {r['query_id']:20}  ERROR: {r['error'][:60]}")
        else:
            print(
                f"  {r['provider']:12} {r['query_id']:20}  "
                f"time={_fmt_time(r.get('pipeline_time_s')):6}  "
                f"agents={len(r.get('active_agents',[]))}  "
                f"ans={len(r.get('final_answer',''))} chars"
            )
