"""Local exercise of safepy_shim (no Anvil, no network).

Run from the repo root:  python scratch_safepy_shim.py
Stubs source_registry with in-memory frames, then drives run_extended through
the cases the endpoint will see: tables, charts, refusals, unknown sources,
and the r/duckdb dialects.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "server_code"))

import pandas as pd

import source_registry

_DF = pd.DataFrame({
    "sex": ["f", "m"] * 40,
    "age": [30 + (i % 25) for i in range(80)],
    "dead": [i % 3 == 0 for i in range(80)],
})

_STUB = {"stub_protected": {"source_id": "stub_protected", "kind": "url",
                            "location": "(stub)", "level": "protected",
                            "default_exec": "remote", "status": "active"}}

source_registry.resolve_source = lambda sid: _STUB[sid]  # KeyError for unknown
source_registry.load_dataframe = lambda src: _DF.copy()

import safepy_shim

SRC = [{"alias": "df", "source_id": "stub_protected"}]
checks = []


def check(name, cond, detail=""):
    checks.append((name, bool(cond), detail))
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))


print("1. pandas table")
r = safepy_shim.run_extended('df.groupby("sex")["age"].mean()', SRC, "pandas")
check("no err", r["err"] is None, repr(r["err"]))
check("one html result", len(r["results"]) == 1 and r["results"][0].startswith("<table"))
check("datasetInfo has df", "df" in r["datasetInfo"] and r["datasetInfo"]["df"]["columns"])

print("2. chart -> figs")
r = safepy_shim.run_extended('df["sex"].value_counts().plot.bar()', SRC, "pandas")
check("no err", r["err"] is None, repr(r["err"]))
check("one fig, parseable plotly json", len(r["figs"]) == 1 and '"data"' in r["figs"][0])

print("3. refusal (raw rows)")
r = safepy_shim.run_extended("df.head()", SRC, "pandas")
check("err set, no results", r["err"] is not None and not r["results"], repr(r["err"]))

print("4. unknown source")
r = safepy_shim.run_extended("df.mean()", [{"alias": "df", "source_id": "nope"}], "pandas")
check("clean unknown-source err", r["err"] == "ukjent kilde: nope", repr(r["err"]))

print("5. r dialect")
r = safepy_shim.run_extended(
    'df %>% group_by(sex) %>% summarise(mean_age = mean(age))', SRC, "r")
check("no err", r["err"] is None, repr(r["err"]))
check("html result", len(r["results"]) == 1)

print("6. duckdb dialect")
r = safepy_shim.run_extended(
    'SELECT sex, avg(age) AS mean_age FROM df GROUP BY sex', SRC, "duckdb")
check("no err", r["err"] is None, repr(r["err"]))
check("html result", len(r["results"]) == 1)

print("7. regression")
r = safepy_shim.run_extended('df.ols(y="age", x=["sex"])', SRC, "pandas")
check("no err", r["err"] is None, repr(r["err"]))
check("regression table", r["results"] and "coef" in r["results"][0], (r["results"] or ["?"])[0][:120])

failed = [c for c in checks if not c[1]]
print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)
