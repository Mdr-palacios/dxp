"""Validate the synthetic Gwinnett voterfile against expected demo properties."""
import sys
from pathlib import Path

import pandas as pd

# Add parent to path so we can import the agent
sys.path.insert(0, str(Path(__file__).parent.parent))

CSV = Path("data/demo/gwinnett_demo_voterfile.csv")
df = pd.read_csv(CSV)

print(f"Total rows: {len(df):,}")
print(f"Columns: {len(df.columns)}")
print()

print("=== Race distribution ===")
print(df["tsmart_race"].value_counts(normalize=True).mul(100).round(1).to_string())
print()

print("=== Party distribution ===")
print(df["party_registration"].value_counts(normalize=True).mul(100).round(1).to_string())
print()

print("=== Gender distribution ===")
print(df["voterbase_gender"].value_counts(normalize=True).mul(100).round(1).to_string())
print()

print("=== Age cohorts ===")
bins = [18, 27, 43, 59, 78, 100]
labels = ["Gen Z (18-26)", "Millennial (27-42)", "Gen X (43-58)", "Boomer (59-77)", "Silent/Greatest (78+)"]
ages = pd.cut(df["voterbase_age"], bins=bins, labels=labels, right=False)
print(ages.value_counts(normalize=True).mul(100).round(1).to_string())
print()

# THE demo target segment
print("=== DEMO TARGET — Latinx voters age 18-35 in Gwinnett ===")
target = df[
    (df["tsmart_race"] == "Hispanic/Latino") &
    (df["voterbase_age"].between(18, 35))
]
print(f"  Count: {len(target):,} ({len(target)/len(df)*100:.1f}% of file)")
print(f"  Avg partisan score (excl. new regs): {pd.to_numeric(target['tsmart_partisan_score'], errors='coerce').mean():.1f}")
print(f"  Avg turnout score (excl. new regs):  {pd.to_numeric(target['tsmart_vote_propensity'], errors='coerce').mean():.1f}")
print(f"  Avg Spanish score:                   {target['tsmart_spanish_language_score'].mean():.1f}")
new_regs = target['tsmart_partisan_score'].astype(str).eq("").sum()
print(f"  New registrants (no scores):         {new_regs:,} ({new_regs/len(target)*100:.1f}% of target)")
print()

print("=== Precinct distribution (top 10) ===")
print(df["precinct_code"].value_counts().head(10).to_string())
print(f"  Unique precincts: {df['precinct_code'].nunique()}")
print()

print("=== Vote-history sanity (2024 turnout by age cohort) ===")
df["_voted_2024"] = df["vote_history_2024"].astype(str).str.upper().eq("TRUE")
cohort_turnout = df.groupby(ages, observed=True)["_voted_2024"].mean().mul(100).round(1)
print(cohort_turnout.to_string())
print()

# Now run through Ben's actual standardize_columns
print("=== Running through chat/agents/voterfile_agent.standardize_columns ===")
from chat.agents.voterfile_agent import standardize_columns, _coerce_columns, _add_derived_columns

std_df, vendor, field_avail = standardize_columns(df)
print(f"Detected vendor: {vendor}")
print(f"Field availability:")
for k, v in sorted(field_avail.items()):
    mark = "OK " if v else "-- "
    print(f"  {mark} {k}")

print()
std_df = _coerce_columns(std_df)
std_df = _add_derived_columns(std_df)

print("=== After full pipeline — derived columns present ===")
derived = [c for c in std_df.columns if c.startswith("_")]
for c in derived:
    print(f"  {c}: {std_df[c].dtype} ({std_df[c].nunique()} unique)")
print()

print("=== Priority cross-tab: High Value (Strong/Persuadable Dem + High/Med-High Turnout) ===")
hv = std_df[
    std_df["_partisan_tier"].isin(["Strong Dem (70-100)", "Persuadable Dem (55-69)"]) &
    std_df["_turnout_tier"].isin(["High (80-100)", "Med-High (60-79)"])
]
print(f"  High Value count: {len(hv):,} ({len(hv)/len(std_df)*100:.1f}% of file)")

hv_target = hv[(hv["_race_norm"] == "Hispanic/Latino") & (hv["age"].between(18, 35))]
print(f"  HV ∩ Latinx 18-35: {len(hv_target):,}")
