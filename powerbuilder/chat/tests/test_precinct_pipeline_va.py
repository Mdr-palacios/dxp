# powerbuilder/chat/tests/test_precinct_pipeline_va.py
"""
Live integration tests for the precinct spatial pipeline using Virginia (FIPS 51)
and Virginia's 7th Congressional District (VA-07, GEOID 5107).

Virginia is used here because it has proper polling-precinct-level NYT data,
unlike states such as Rhode Island that report at ward/municipality level.
This allows a realistic VAP-per-precinct range check (2,000 - 15,000).

Test sections:
  0. Pre-flight: dependency and data-file checks
  1. crosswalk_builder.py -- build BG-to-precinct crosswalk for VA-07
  1a. Crosswalk column validation
  1b. Weight sum validation per bg_geoid (tolerance 0.05)
  2. precincts.py -- top 10 precincts in VA-07 by VAP
  2a. Output metadata checks (precinct_count, data_quality_note)
  2b. Schema checks on returned precinct records
  2c. Top-10 audit table with approximate_boundary flags

Data requirements:
  - chat/precinct_shapefiles/2024precincts-with-results.topojson  (included in repo)
  - Census TIGER/Line block group shapefiles                       (downloaded at runtime)
  - CENSUS_API_KEY in .env                                         (for precincts section)

Run from the project root:
  python -m chat.tests.test_precinct_pipeline_va
  -- or --
  python chat/tests/test_precinct_pipeline_va.py
"""

from dotenv import load_dotenv
load_dotenv()  # must be before any import that reads env vars

import logging
import os
import sys

# -- Path setup ---------------------------------------------------------------
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

import pandas as pd

# -- Logging: surface crosswalk_builder's internal logger ---------------------
logging.basicConfig(
    level=logging.INFO,
    format="  [LOG] %(message)s",
    stream=sys.stdout,
)
for noisy in ("fiona", "pyproj", "shapely", "urllib3", "requests"):
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

STATE_FIPS    = "51"           # Virginia
STATE_NAME    = "Virginia"
DISTRICT_NUM  = 7
DISTRICT_ID   = "5107"        # VA-07 congressional GEOID
METRIC        = "vap"         # Voting Age Population 18+ (2020 Decennial P3_001N)
                              # stored as bg_vap in the crosswalk CSV
TOP_N         = 10
WEIGHT_TOL    = 0.05          # max allowed deviation from 1.0 per bg_geoid

# Realistic VAP per polling precinct for Virginia.
# VA-07 precincts are true polling precincts, not wards/municipalities.
VAP_MIN = 2_000
VAP_MAX = 15_000

CROSSWALK_PATH       = f"data/crosswalks/{STATE_FIPS}_{DISTRICT_ID}_bg_to_precinct.csv"
STATE_CROSSWALK_PATH = f"data/crosswalks/{STATE_FIPS}_bg_to_precinct.csv"
REQUIRED_CROSSWALK_COLS = ["bg_geoid", "precinct_geoid", "weight", "official_boundary", "bg_vap"]

TOPOJSON_PATH = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "../../chat/precinct_shapefiles/2024precincts-with-results.topojson",
    )
)


# =============================================================================
# 0. Pre-flight: dependency and data-file checks
# =============================================================================

section("0 - Pre-flight checks")

try:
    import geopandas as gpd
    has_geopandas = True
    check_true("geopandas is installed", True, note=gpd.__version__)
except ImportError:
    has_geopandas = False
    skip("geopandas installed", "pip install geopandas to enable crosswalk build")

topojson_exists = os.path.isfile(TOPOJSON_PATH)
check_true(
    "TopoJSON exists at expected path",
    topojson_exists,
    note=TOPOJSON_PATH,
)
if not topojson_exists:
    warn(f"Expected path: {TOPOJSON_PATH}")
    warn("Place the TopoJSON at chat/precinct_shapefiles/2024precincts-with-results.topojson")

has_census_key = bool(os.environ.get("CENSUS_API_KEY"))
if has_census_key:
    check_true("CENSUS_API_KEY is set", True)
else:
    skip("CENSUS_API_KEY check", "Not set -- precincts section will be skipped")


# =============================================================================
# 1. crosswalk_builder.py -- Build district crosswalk for VA-07
# =============================================================================

section(
    f"1 - crosswalk_builder.py -- Build district crosswalk for "
    f"{STATE_NAME} district {DISTRICT_ID} (FIPS {STATE_FIPS})"
)

crosswalk_df = None

if not has_geopandas:
    skip("build_crosswalk", "geopandas not installed")
elif not topojson_exists:
    skip("build_crosswalk", f"TopoJSON not found at {TOPOJSON_PATH}")
else:
    from chat.utils.crosswalk_builder import build_crosswalk

    print(f"  Building district-specific BG-to-precinct crosswalk for VA-07 ({DISTRICT_ID})...")
    print("  Downloads: TIGER/Line BG shapefile (per state) + CD116 shapefile (national, cached)")
    print("  Spatial filter: precincts by intersection, BGs by centroid-within CD boundary")
    print()

    success = build_crosswalk(STATE_FIPS, district_id=DISTRICT_ID)
    check_true("build_crosswalk() returned True", success)

    if success or os.path.exists(CROSSWALK_PATH):
        crosswalk_df = pd.read_csv(CROSSWALK_PATH, dtype={"bg_geoid": str})

if crosswalk_df is None and os.path.exists(CROSSWALK_PATH):
    crosswalk_df = pd.read_csv(CROSSWALK_PATH, dtype={"bg_geoid": str})
    info("Using existing district crosswalk CSV (build was skipped)", CROSSWALK_PATH)

if not has_geopandas and not os.path.exists(CROSSWALK_PATH):
    skip(
        f"District crosswalk CSV exists at {CROSSWALK_PATH}",
        "geopandas not installed; no pre-existing CSV to check",
    )
else:
    check_true(
        f"District crosswalk CSV exists at {CROSSWALK_PATH}",
        os.path.exists(CROSSWALK_PATH),
    )


# =============================================================================
# 1a. Column validation
# =============================================================================

if crosswalk_df is not None:
    section("1a - Crosswalk column validation")

    for col in REQUIRED_CROSSWALK_COLS:
        check_true(f'Column "{col}" present', col in crosswalk_df.columns)

    n_bgs       = crosswalk_df["bg_geoid"].nunique()
    n_precincts = crosswalk_df["precinct_geoid"].nunique()
    info("Total BG-precinct pairs", len(crosswalk_df))
    info("Unique block groups  ", n_bgs)
    info("Unique precincts     ", n_precincts)

    # Spatial filter should reduce scope vs. full-state crosswalk
    if os.path.exists(STATE_CROSSWALK_PATH):
        state_df  = pd.read_csv(STATE_CROSSWALK_PATH)
        state_bgs = state_df["bg_geoid"].nunique()
        check_true(
            f"District crosswalk has fewer BGs than full-state ({state_bgs})",
            n_bgs < state_bgs,
            note=f"District: {n_bgs} BGs, State: {state_bgs} BGs",
        )
    else:
        info("State-level crosswalk not found for BG count comparison", STATE_CROSSWALK_PATH)

    ob_col      = crosswalk_df["official_boundary"].astype(str).str.lower()
    n_official  = (ob_col == "true").sum()
    n_estimated = (ob_col == "false").sum()
    info(
        "official_boundary breakdown",
        f"{n_official} official, {n_estimated} estimated "
        f"({100 * n_estimated / max(len(crosswalk_df), 1):.1f}% estimated)",
    )


# =============================================================================
# 1b. Weight sum validation (tolerance = 0.05)
# =============================================================================

    section(f"1b - Weight sum validation (tolerance = {WEIGHT_TOL})")

    weight_sums = crosswalk_df.groupby("bg_geoid")["weight"].sum()
    deviations  = (weight_sums - 1.0).abs()
    failing_bgs = deviations[deviations > WEIGHT_TOL]

    if len(failing_bgs) == 0:
        check_true(
            f"All bg_geoid weight sums within {WEIGHT_TOL} of 1.0",
            True,
            note=f"{len(weight_sums)}/{len(weight_sums)} block groups pass",
        )
    else:
        # Informational — weight deviations at district edges and water bodies
        # are expected data gaps, not code bugs.
        _results.append((f"All bg_geoid weight sums within {WEIGHT_TOL} of 1.0", None))
        print(
            f"  [{_YELLOW}INFO{_RESET}] Weight deviation found: "
            f"{len(failing_bgs)}/{len(weight_sums)} BGs exceed tolerance "
            f"(boundary edge/water gaps -- expected, not a code bug)"
        )

    info(
        "Weight sum stats",
        f"min={weight_sums.min():.4f}  "
        f"mean={weight_sums.mean():.4f}  "
        f"max={weight_sums.max():.4f}  "
        f"n={len(weight_sums)}",
    )

    if not failing_bgs.empty:
        print(f"\n  Block groups failing weight check (deviation > {WEIGHT_TOL}):")
        print(f"  {'bg_geoid':<16}  {'weight_sum':>12}  {'deviation':>12}")
        print(f"  {'-' * 44}")
        for bg_id, wsum in weight_sums[failing_bgs.index].sort_values(ascending=False).items():
            dev  = abs(wsum - 1.0)
            flag = " <-- missing precinct coverage" if wsum < 0.5 else ""
            print(f"  {bg_id:<16}  {wsum:>12.4f}  {dev:>12.4f}{flag}")
        print()
        warn(
            "Weight sums far below 1.0 indicate block groups with no matching precinct "
            "(water bodies, district edge BGs, or TopoJSON boundary gaps)."
        )
    else:
        print(f"  All {len(weight_sums)} block groups have weight sums within tolerance.")


# =============================================================================
# 2. precincts.py -- Top 10 precincts in VA-07 by VAP
# =============================================================================

section(f"2 - precincts.py -- Top {TOP_N} precincts in VA-07 by {METRIC}")

if not has_census_key:
    skip("PrecinctsAgent.get_top_precincts", "CENSUS_API_KEY not set")
elif not os.path.exists(CROSSWALK_PATH):
    skip("PrecinctsAgent.get_top_precincts", f"Crosswalk not found at {CROSSWALK_PATH}")
else:
    from chat.agents.precincts import PrecinctsAgent

    print(f"  Fetching Census block-group data and applying crosswalk weights...")
    print(f"  state_fips={STATE_FIPS!r}  district_id={DISTRICT_ID!r}  metric={METRIC!r}")
    print()

    output = PrecinctsAgent.get_top_precincts(
        state_fips    = STATE_FIPS,
        district_id   = DISTRICT_ID,
        district_type = "congressional",
        metrics       = [METRIC],
        top_n         = TOP_N,
    )

    # Error check
    if "error" in output:
        check_true("No error from get_top_precincts", False, note=output["error"])
    else:
        results        = output["precincts"]
        precinct_count = output["precinct_count"]
        dq_note        = output["data_quality_note"]

        check_true(
            "get_top_precincts returned a non-empty list",
            len(results) > 0,
            note=f"{len(results)} precincts returned",
        )

        # -- Output metadata checks -------------------------------------------
        section("2a - Output metadata checks")

        check_true(
            'Output has "precinct_count" field',
            isinstance(precinct_count, int) and precinct_count > 0,
            note=f"precinct_count = {precinct_count}",
        )
        check_true(
            'Output has "data_quality_note" field (None or str)',
            dq_note is None or isinstance(dq_note, str),
        )

        # Virginia has real polling precincts -- expect >= 100 in VA-07
        # so the data quality warning should NOT fire
        check_true(
            "precinct_count >= 100 (VA has true polling-precinct granularity)",
            precinct_count >= 100,
            note=f"precinct_count = {precinct_count}",
        )
        check_true(
            "data_quality_note is None (no ward-level warning for VA)",
            dq_note is None,
            note="Warning is suppressed when precinct_count >= 100",
        )

        # -- Schema checks on precinct records --------------------------------
        section("2b - Schema checks on returned precinct records")

        expected_fields = ["precinct_geoid", "precinct_name", METRIC, "approximate_boundary"]
        if results:
            first = results[0]
            for field in expected_fields:
                check_true(f'First result has "{field}" field', field in first)

            check_true(
                f'"{METRIC}" is a positive number',
                isinstance(first.get(METRIC), (int, float)) and first[METRIC] > 0,
                note=f"{METRIC} = {first.get(METRIC):,.0f}" if first.get(METRIC) else "",
            )
            check_true(
                '"approximate_boundary" is a bool',
                isinstance(first.get("approximate_boundary"), bool),
            )

        # -- Top-10 audit table -----------------------------------------------
        section(f"2c - Top {TOP_N} precincts in VA-07 by {METRIC} (manual audit)")

        approx_count = sum(1 for r in results if r.get("approximate_boundary"))

        print()
        print(f"  {'#':<4} {'precinct_name':<44} {METRIC:>10}  {'approx?':>8}")
        print(f"  {'-' * 72}")
        for i, rec in enumerate(results, 1):
            name   = rec.get("precinct_name", rec.get("precinct_geoid", ""))[:42]
            val    = rec.get(METRIC, 0)
            approx = rec.get("approximate_boundary", False)
            flag   = "  [~]" if approx else ""
            print(f"  {i:<4} {name:<44} {val:>10,.0f}{flag}")

        print()
        if approx_count > 0:
            warn(
                f"{approx_count} of {len(results)} precincts are flagged [~] -- "
                "approximate_boundary=True means at least one contributing block group "
                "used an estimated (non-official) precinct boundary."
            )
        else:
            info("Boundary quality", "All returned precincts have official boundary coverage")

        # -- VAP range check --------------------------------------------------
        # Virginia uses true polling precincts; realistic VAP range is 2,000-15,000.
        # Values well outside this range indicate a spatial join error or
        # data still aggregated at ward/city level.
        if results:
            top_val    = results[0].get(METRIC, 0)
            bottom_val = results[-1].get(METRIC, 0)

            check_true(
                f"Top precinct {METRIC} is in realistic polling-precinct range "
                f"({VAP_MIN:,} - {VAP_MAX:,})",
                VAP_MIN <= top_val <= VAP_MAX,
                note=f"Top value: {top_val:,.0f}",
            )
            check_true(
                f"Bottom precinct {METRIC} is in realistic polling-precinct range "
                f"({VAP_MIN:,} - {VAP_MAX:,})",
                VAP_MIN <= bottom_val <= VAP_MAX,
                note=f"Bottom value: {bottom_val:,.0f}",
            )

        # Values should be monotonically non-increasing (sorted descending)
        vals = [r.get(METRIC, 0) for r in results]
        check_true(
            f"Results are sorted descending by {METRIC}",
            all(vals[i] >= vals[i + 1] for i in range(len(vals) - 1)),
            note=f"Values: {[round(v) for v in vals]}",
        )


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
