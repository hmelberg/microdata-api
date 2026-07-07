# ============================================================================
# GENERATED COPY — DO NOT EDIT HERE.
# Source of truth: the m2py repo. This file is produced by sync_to_api.py.
# Edit the engine in the m2py repo and re-run that script; direct edits here
# are overwritten on the next sync.
# ============================================================================
"""
mockdata_core.py — shared primitives for synthetic data generation in m2py.

This module holds small, reusable helpers used by both the legacy mock-data
paths in m2py.py and the new realism framework in mockdata_realism.py:

    - latent_z(unit_id)            deterministic N(0,1) per person
    - unit_seed(unit_id, salt)     deterministic RNG seed per (person, purpose)
    - map_nus2000_to_level(code)   NUS2000 education code -> "low"/"medium"/"high"
    - synth_education(...)         deterministic education level per unit_id,
                                   conditional on age and the import date

Constants (_DEMO_REF_YEAR, _NORWAY_LATENT_*) live here — mockdata_realism.py
imports them without depending on m2py, and m2py.py imports them from here
too (no longer duplicated as of 2026-07-07; this module has no m2py
dependency, so the import direction is one-way and safe).

Pyodide note: stdlib + numpy only. No pandas dependency at import time.
"""

from __future__ import annotations

import hashlib
import numpy as np


# ---------------------------------------------------------------------------
# Constants (source of truth — m2py.py imports these, not the other way)
# ---------------------------------------------------------------------------

_DEMO_REF_YEAR = 2025

_NORWAY_LATENT_LOG_WAGE = 0.22
_NORWAY_LATENT_LOG_WEALTH_NET = 0.52
_NORWAY_LATENT_LOG_WEALTH_GROSS = 0.44
_NORWAY_LATENT_LOG_INCOME_OTHER = 0.15
_NORWAY_LATENT_TRANSFER_HURDLE_SHIFT = 0.04


# ---------------------------------------------------------------------------
# Latent factor and unit seeding (mirror m2py.py:1040-1082)
# ---------------------------------------------------------------------------

def latent_z(unit_id) -> float:
    """Deterministic standard-normal draw keyed on unit_id.

    Uses the same hash salt ("norway_latent_v1") as m2py._norway_latent_z so
    that values generated via the new pipeline match the legacy pipeline for
    the same person.
    """
    h = hashlib.md5(f"norway_latent_v1:{int(unit_id)}".encode()).digest()
    u1 = int.from_bytes(h[:4], "big") / 2**32
    u2 = int.from_bytes(h[4:8], "big") / 2**32
    u1 = max(1e-12, min(1.0 - 1e-12, u1))
    u2 = max(1e-12, min(1.0 - 1e-12, u2))
    return float(np.sqrt(-2.0 * np.log(u1)) * np.cos(2.0 * np.pi * u2))


def unit_seed(unit_id, salt: str) -> int:
    """32-bit RNG seed deterministic in (unit_id, salt). Same as m2py._norway_demo_unit_seed."""
    return int(hashlib.md5(f"{salt}:{int(unit_id)}".encode()).hexdigest(), 16) % (2**32)


def latent_z_vec(unit_ids) -> np.ndarray:
    """Vectorised latent_z for arrays of unit_ids. Thin wrapper for convenience."""
    return np.array([latent_z(u) for u in unit_ids], dtype=float)


# ---------------------------------------------------------------------------
# Education: NUS2000 code -> ordinal level
# ---------------------------------------------------------------------------

# NUS2000 first digit convention (from codelists/NUDB_BU.json labels):
#   0 -> no/pre-school education
#   1 -> primary (barneskole)
#   2 -> lower secondary (ungdomsskole)
#   3 -> upper secondary (videregående grunnutdanning)
#   4 -> upper secondary final (videregående avsluttende)
#   5 -> post-secondary non-tertiary (påbygging)
#   6 -> tertiary short (university/college, bachelor-level)
#   7 -> tertiary long (master-level)
#   8 -> tertiary research (PhD)
#   9 -> unspecified

_NUS2000_LEVEL_BY_FIRST_DIGIT = {
    "0": "low", "1": "low", "2": "low",
    "3": "medium", "4": "medium", "5": "medium",
    "6": "high", "7": "high", "8": "high",
}

# Ordinal rank for each level (used when applying verb effects where education
# acts as an ordinal driver).
EDUCATION_LEVEL_RANK = {"low": 0, "medium": 1, "high": 2}


def map_nus2000_to_level(code) -> str:
    """Return 'low' / 'medium' / 'high' for an NUS2000 code, or 'medium' as fallback.

    Accepts strings, ints, or None. Padded codes like '099903' and short codes
    like '6' both work — only the first digit is consulted.
    """
    if code is None:
        return "medium"
    s = str(code).strip()
    if not s:
        return "medium"
    # Strip leading zeros except when the whole code is zeros
    first = s[0]
    return _NUS2000_LEVEL_BY_FIRST_DIGIT.get(first, "medium")


# ---------------------------------------------------------------------------
# Synthetic education level (used when NUDB_BU not imported)
# ---------------------------------------------------------------------------

# Age/date-conditional prior on education level. Rows are birth cohorts
# (approx: age at import date), columns are ("low", "medium", "high").
# Numbers are rough and will be tuned against Norwegian register data later.
#
# Older cohorts skew lower-educated; younger skew higher. The prior shifts
# slightly upward for more recent import dates (overall attainment has risen).

_EDU_PRIOR_BY_BIRTH_COHORT = {
    # birth_year_upper_bound: (low, medium, high)
    1945: (0.55, 0.35, 0.10),
    1960: (0.35, 0.40, 0.25),
    1975: (0.20, 0.45, 0.35),
    1990: (0.12, 0.45, 0.43),
    2005: (0.10, 0.45, 0.45),
    9999: (0.15, 0.55, 0.30),  # fallback for very young / very old
}


def _edu_prior_for_birth_year(birth_year: int) -> tuple:
    """Pick the first cohort bucket whose upper bound >= birth_year."""
    for ub in sorted(_EDU_PRIOR_BY_BIRTH_COHORT.keys()):
        if birth_year <= ub:
            return _EDU_PRIOR_BY_BIRTH_COHORT[ub]
    return _EDU_PRIOR_BY_BIRTH_COHORT[9999]


def synth_education(unit_id, age: int | None = None, as_of_year: int | None = None) -> str:
    """Deterministic education level ('low'/'medium'/'high') for a unit_id.

    If `age` is known, uses a birth-cohort-conditional prior. Without age, uses
    a uniform prior. `as_of_year` is accepted but not yet used — reserved for
    later date-conditional calibration.

    Stable across calls for the same unit_id (seeded hash).
    """
    # Birth year from age and reference year (fallback: treat unit as working-age)
    ref_year = int(as_of_year) if as_of_year is not None else _DEMO_REF_YEAR
    if age is None:
        birth_year = ref_year - 40  # working-age fallback
    else:
        birth_year = ref_year - int(age)
        # Children/teens can't yet hold a NUS2000 attainment level — without
        # this guard, birth years > 2005 fall into the youngest cohort bucket
        # (9999), which still assigns 30% "high" (tertiary) to 10-year-olds.
        if int(age) < 18:
            return "low"

    probs = _edu_prior_for_birth_year(birth_year)

    rng = np.random.default_rng(unit_seed(unit_id, "education"))
    u = float(rng.random())
    cum = 0.0
    for level, p in zip(("low", "medium", "high"), probs):
        cum += p
        if u < cum:
            return level
    return "high"


def synth_education_vec(unit_ids, ages=None, as_of_year=None) -> np.ndarray:
    """Vectorised synth_education returning an object array of level strings."""
    n = len(unit_ids)
    out = np.empty(n, dtype=object)
    if ages is None:
        for i, uid in enumerate(unit_ids):
            out[i] = synth_education(uid, None, as_of_year)
    else:
        for i, (uid, a) in enumerate(zip(unit_ids, ages)):
            out[i] = synth_education(uid, a, as_of_year)
    return out
