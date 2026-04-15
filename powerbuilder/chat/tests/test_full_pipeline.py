"""
test_full_pipeline.py

Live end-to-end integration test for the Powerbuilder manager pipeline.
No mocking. All three queries invoke manager_app directly.

Run from the project root:
    python -m pytest chat/tests/test_full_pipeline.py -v -s
or:
    python chat/tests/test_full_pipeline.py
"""

import os
import sys

# load_dotenv MUST come before any imports that use API keys
from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import pytest

# ---------------------------------------------------------------------------
# Non-fatal error fragments — these are expected and should not fail the test
# ---------------------------------------------------------------------------
NON_FATAL_PATTERNS = [
    "FEC",
    "District filter unavailable",
    "2022 cycle",
    "PrecinctsAgent: Data quality",
    "PrecinctsAgent: Census API",
    "Census API failure",
    "Cook",
    "MEDSL",
]

def is_fatal_error(err: str) -> bool:
    """Returns True if the error string is unexpected / fatal."""
    for pattern in NON_FATAL_PATTERNS:
        if pattern in err:
            return False
    return True


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
def run_query(query: str, label: str) -> dict:
    """Invoke the manager pipeline and return the final state."""
    from chat.agents.manager import manager_app

    initial_state = {
        "query":        query,
        "org_namespace": "general",
    }

    print(f"\n{'='*70}")
    print(f"QUERY: {label}")
    print(f"  \"{query}\"")
    print(f"{'='*70}")

    result = manager_app.invoke(
        initial_state,
        config={"recursion_limit": 50},
    )

    active = result.get("active_agents", [])
    errors = result.get("errors", [])
    answer = result.get("final_answer", "")

    print(f"\n[Active agents] {active}")

    if errors:
        print(f"\n[Errors ({len(errors)})]")
        for e in errors:
            tag = "NON-FATAL" if not is_fatal_error(e) else "FATAL"
            print(f"  [{tag}] {e}")
    else:
        print("\n[Errors] none")

    print(f"\n[Final Answer]\n{answer}\n")

    return result


# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
class TestSection0PreFlight:

    def test_api_keys_present(self):
        """Required environment variables must be set."""
        for key in ("OPENAI_API_KEY", "PINECONE_API_KEY", "OPENAI_PINECONE_INDEX_NAME"):
            assert os.getenv(key), f"Missing env var: {key}"

    def test_crosswalk_file_exists(self):
        """Precincts agent needs a BG->precinct crosswalk for VA-07 (FIPS 51)."""
        crosswalk = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "data", "crosswalks", "51_5107_bg_to_precinct.csv",
        )
        assert os.path.exists(crosswalk), (
            f"Missing crosswalk: {crosswalk}\n"
            "Run test_precinct_pipeline_va.py first to generate it."
        )

    def test_election_data_exists(self):
        """Election results agent needs 51_master.csv for VA."""
        master = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "data", "election_results", "51_master.csv",
        )
        assert os.path.exists(master), (
            f"Missing election master: {master}\n"
            "Run test_election_pipeline.py for VA first."
        )

    def test_manager_app_imports(self):
        """manager_app must compile without errors."""
        from chat.agents.manager import manager_app
        assert manager_app is not None


# ---------------------------------------------------------------------------
# Query 1 — Young voter outreach / precinct targeting / messaging
# ---------------------------------------------------------------------------
class TestSection1YoungVoters:

    @pytest.fixture(scope="class")
    def result(self):
        query = (
            "I want to reach young voters in Virginia's 7th Congressional District. "
            "What precincts should I target and what message should I deliver?"
        )
        return run_query(query, "Q1: Young voters / precincts / messaging")

    def test_has_final_answer(self, result):
        answer = result.get("final_answer", "")
        assert len(answer) > 100, "final_answer is too short or empty"

    def test_precincts_agent_ran(self, result):
        active = result.get("active_agents", [])
        assert "precincts" in active, (
            f"'precincts' agent did not run. Active: {active}"
        )

    def test_messaging_agent_ran(self, result):
        active = result.get("active_agents", [])
        assert "messaging" in active, (
            f"'messaging' agent did not run. Active: {active}"
        )

    def test_no_fatal_errors(self, result):
        errors = result.get("errors", [])
        fatal = [e for e in errors if is_fatal_error(e)]
        assert not fatal, f"Fatal errors found: {fatal}"


# ---------------------------------------------------------------------------
# Query 2 — Canvassing cost estimate
# ---------------------------------------------------------------------------
class TestSection2CanvassingCost:

    @pytest.fixture(scope="class")
    def result(self):
        query = (
            "How much would it cost to run a canvassing program "
            "in Virginia's 7th Congressional District in 2026?"
        )
        return run_query(query, "Q2: Canvassing cost estimate")

    def test_has_final_answer(self, result):
        answer = result.get("final_answer", "")
        assert len(answer) > 100, "final_answer is too short or empty"

    def test_cost_calculator_ran(self, result):
        active = result.get("active_agents", [])
        assert "cost_calculator" in active, (
            f"'cost_calculator' agent did not run. Active: {active}"
        )

    def test_no_fatal_errors(self, result):
        errors = result.get("errors", [])
        fatal = [e for e in errors if is_fatal_error(e)]
        assert not fatal, f"Fatal errors found: {fatal}"


# ---------------------------------------------------------------------------
# Query 3 — Win number
# ---------------------------------------------------------------------------
class TestSection3WinNumber:

    @pytest.fixture(scope="class")
    def result(self):
        query = (
            "What is the win number for Virginia's 7th Congressional District in 2026?"
        )
        return run_query(query, "Q3: Win number")

    def test_has_final_answer(self, result):
        answer = result.get("final_answer", "")
        assert len(answer) > 50, "final_answer is too short or empty"

    def test_win_number_agent_ran(self, result):
        active = result.get("active_agents", [])
        assert "win_number" in active, (
            f"'win_number' agent did not run. Active: {active}"
        )

    def test_no_fatal_errors(self, result):
        errors = result.get("errors", [])
        fatal = [e for e in errors if is_fatal_error(e)]
        assert not fatal, f"Fatal errors found: {fatal}"


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("\n=== Powerbuilder Full Pipeline Integration Test ===\n")

    # Pre-flight
    for key in ("OPENAI_API_KEY", "PINECONE_API_KEY", "OPENAI_PINECONE_INDEX_NAME"):
        val = os.getenv(key)
        status = "OK" if val else "MISSING"
        print(f"  {key}: {status}")

    crosswalk = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "data", "crosswalks", "51_5107_bg_to_precinct.csv",
    )
    master = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "data", "election_results", "51_master.csv",
    )
    print(f"  Crosswalk (VA-51): {'EXISTS' if os.path.exists(crosswalk) else 'MISSING'}")
    print(f"  Election master (VA-51): {'EXISTS' if os.path.exists(master) else 'MISSING'}")

    queries = [
        (
            "I want to reach young voters in Virginia's 7th Congressional District. "
            "What precincts should I target and what message should I deliver?",
            "Q1: Young voters / precincts / messaging",
            ["precincts", "messaging"],
        ),
        (
            "How much would it cost to run a canvassing program "
            "in Virginia's 7th Congressional District in 2026?",
            "Q2: Canvassing cost estimate",
            ["cost_calculator"],
        ),
        (
            "What is the win number for Virginia's 7th Congressional District in 2026?",
            "Q3: Win number",
            ["win_number"],
        ),
    ]

    all_passed = True

    for query_text, label, required_agents in queries:
        result = run_query(query_text, label)

        active  = result.get("active_agents", [])
        errors  = result.get("errors", [])
        answer  = result.get("final_answer", "")
        fatal   = [e for e in errors if is_fatal_error(e)]
        missing = [a for a in required_agents if a not in active]

        checks = {
            "final_answer non-empty":     len(answer) > 50,
            "required agents ran":        len(missing) == 0,
            "no fatal errors":            len(fatal) == 0,
        }

        print(f"\n--- Results for {label} ---")
        for check, passed in checks.items():
            icon = "PASS" if passed else "FAIL"
            print(f"  [{icon}] {check}")
            if not passed:
                all_passed = False
                if check == "required agents ran":
                    print(f"         Missing: {missing}")
                if check == "no fatal errors":
                    for fe in fatal:
                        print(f"         FATAL: {fe}")

    print(f"\n{'='*70}")
    print(f"  Overall: {'ALL PASSED' if all_passed else 'SOME FAILURES'}")
    print(f"{'='*70}\n")
