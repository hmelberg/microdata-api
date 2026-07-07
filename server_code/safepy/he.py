# ============================================================================
# GENERATED COPY — DO NOT EDIT HERE.
# Source of truth: the safepy repo. This file is produced by sync_to_api.py.
# Edit the engine in the safepy repo and re-run that script; direct edits here
# are overwritten on the next sync.
# ============================================================================
"""Plane B — the HE track: Paillier-encrypted datasets and the gated release
authority (spec: docs/superpowers/specs/2026-07-04-homomorphic-release-design.md §3).

Layout trick that avoids FHE circuits entirely: *value* columns are encrypted
(additively homomorphic Paillier), *group-by/filter* columns stay plaintext but
coarsened. Group-by-sum is then just adding ciphertexts within each plaintext
partition; the release authority decrypts only cells whose plaintext unit count
passed the k-gate, and feeds the decrypted ``(table, counts)`` into the existing
backend-neutral audited suppressors in :mod:`safepy.safe`.

Encoding spec (interop contract with the browser encryptor, `paillier-bigint`):
  - floats are fixed-point encoded: ``enc_int = round(x * scale)`` with a per-
    column integer ``scale`` (default 10**6); decrypt then divide by ``scale``.
  - the squared column encrypts ``round(x * scale) ** 2`` (i.e. carries scale²).
  - NaN cells encrypt 0 with validity-mask 0; non-null count = Σ mask.
  - ciphertexts travel as hex strings of the raw Paillier ciphertext integer
    (exponent 0 — only python ints are ever encrypted).

Trust model: ``encrypt_dataframe`` runs on the *owner's* machine (reference
implementation; the browser encryptor is the production path).
``blind_group_agg`` needs only the public key — an untrusted node can run it.
``HEAuthority`` is the only holder of the private key; it never decrypts a cell
below the suppression threshold.
"""

from __future__ import annotations

import hashlib
import json

import numpy as np
import pandas as pd

from .errors import DisclosureError
from .policy import Policy
from .result import Released
from .safe import SafeVerbs, _agg_min_n

try:
    import phe
except ImportError:  # pragma: no cover
    phe = None

FORMAT = "safepy-he-v1"
DEFAULT_SCALE = 10 ** 6

# Aggregates computable from (Σx, Σx², Σmask, group size) alone. Order
# statistics (median) are impossible under additive HE — refused with guidance.
_HE_AGGS = frozenset({"mean", "sum", "count", "size", "std", "var"})


def _require_phe():
    if phe is None:
        raise ImportError(
            "the HE plane requires the 'phe' package (pip install 'safepy[he]')")


def _encode(x, scale: int) -> int:
    """Fixed-point encode one value (see encoding spec in the module docstring)."""
    return int(round(float(x) * scale))


def _hex(n: int) -> str:
    return format(n, "x")


def _load_public(ds: dict):
    _require_phe()
    return phe.paillier.PaillierPublicKey(int(ds["public_key"]["n"], 16))


# --- scheme-pure Paillier primitives (browser-interoperable) -----------------
# The wire ciphertext is the RAW Paillier integer c = (n+1)^m · r^n mod n²,
# hex-encoded — NOT phe's EncodedNumber wrapper, which the browser lib
# (paillier-bigint) has no notion of. paillier-bigint must encrypt with g = n+1
# (its default g is random and would NOT decrypt here). Plaintexts are the
# fixed-point encoded ints reduced mod n; decryption maps the upper half of
# [0, n) back to negatives. Homomorphic addition is ciphertext multiplication
# mod n², which phe's EncryptedNumber(+) performs at exponent 0.

def _enc_hex(pub, m: int) -> str:
    """Raw-encrypt one integer (reduced mod n) -> hex ciphertext."""
    return _hex(pub.raw_encrypt(int(m) % pub.n))


def _ct(pub, hexct: str):
    """Rebuild a phe EncryptedNumber (exponent 0) from a raw hex ciphertext."""
    return phe.paillier.EncryptedNumber(pub, int(hexct, 16), 0)


def _ct_hex(en) -> str:
    """Deterministic raw hex of an EncryptedNumber (no re-obfuscation)."""
    return _hex(en.ciphertext(be_secure=False))


def _decrypt_int(priv, en) -> int:
    """Raw-decrypt an EncryptedNumber and map the upper half back to negatives."""
    n = priv.public_key.n
    m = priv.raw_decrypt(en.ciphertext(be_secure=False))
    return m - n if m > n // 2 else m


def encrypt_dataframe(df: pd.DataFrame, *, value_cols, group_cols,
                      scale: int = DEFAULT_SCALE, key_bits: int = 2048,
                      winsorize=None):
    """Owner-side reference encryptor (the browser encryptor is the production
    path; this one pins the format and enables tests). Returns ``(dataset,
    private_key)`` — the private key never leaves the owner/authority side.

    ``winsorize=(low, high)`` percentile-clips each value column BEFORE
    encryption (ciphertext cannot be clipped later) and records it so the
    authority can check the policy's demand against what the data carries.
    """
    _require_phe()
    pub, priv = phe.paillier.generate_paillier_keypair(n_length=key_bits)
    for c in list(group_cols) + list(value_cols):
        if c not in df.columns:
            raise DisclosureError(f"unknown column: {c}")
    ds = {
        "format": FORMAT,
        "n_rows": int(len(df)),
        "public_key": {"n": _hex(pub.n)},
        "group_columns": {c: [None if pd.isna(v) else str(v) for v in df[c]]
                          for c in group_cols},
        "value_columns": {},
    }
    for c in value_cols:
        vals = pd.to_numeric(df[c])
        if winsorize is not None:
            lo, hi = vals.quantile(float(winsorize[0])), vals.quantile(float(winsorize[1]))
            vals = vals.clip(lo, hi)
        mask = (~vals.isna()).astype(int)
        ints = [_encode(v, scale) if m else 0 for v, m in zip(vals.fillna(0.0), mask)]
        ds["value_columns"][c] = {
            "scale": int(scale),
            "ct": [_enc_hex(pub, i) for i in ints],
            "ct_sq": [_enc_hex(pub, i * i) for i in ints],
            "mask": [_enc_hex(pub, int(m)) for m in mask],
            "winsorize": None if winsorize is None else [float(winsorize[0]), float(winsorize[1])],
        }
    return ds, priv


def dataset_fingerprint(ds: dict) -> str:
    """Content hash for registration (file-swap protection / cache key)."""
    canon = json.dumps(ds, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def serialize_private_key(priv) -> dict:
    """JSON-safe form of the authority's Paillier private key ({p, q, n} hex).
    The server stores this encrypted at rest; it is never sent to a client."""
    return {"p": _hex(priv.p), "q": _hex(priv.q), "n": _hex(priv.public_key.n)}


def load_private_key(d: dict):
    """Rebuild a Paillier private key from :func:`serialize_private_key` output,
    checking the factors against the recorded modulus."""
    _require_phe()
    p, q, n = int(d["p"], 16), int(d["q"], 16), int(d["n"], 16)
    if p * q != n:
        raise DisclosureError("private key rejected: factors do not match the modulus")
    return phe.paillier.PaillierPrivateKey(phe.paillier.PaillierPublicKey(n), p, q)


def _group_rows(ds: dict, by) -> tuple[list, dict]:
    """Partition row indices by the plaintext group column(s). Rows whose key
    contains a missing label are dropped (mirrors pandas groupby dropna)."""
    by = [by] if isinstance(by, str) else list(by)
    for b in by:
        if b not in ds["group_columns"]:
            raise DisclosureError(
                f"'{b}' is not a plaintext group column of this encrypted dataset; "
                f"grouping/filtering is only possible on {sorted(ds['group_columns'])}")
    cols = [ds["group_columns"][b] for b in by]
    rows: dict = {}
    for i, labels in enumerate(zip(*cols)):
        if any(l is None for l in labels):
            continue
        key = labels[0] if len(by) == 1 else labels
        rows.setdefault(key, []).append(i)
    return by, rows


def blind_group_agg(ds: dict, by, value: str) -> dict:
    """Ciphertext-only grouped aggregation: per group, the encrypted Σx, Σx² and
    Σmask plus the plaintext unit count. Requires only the PUBLIC key — an
    untrusted node can run this; nothing here can be read without the authority.
    """
    pub = _load_public(ds)
    if value not in ds["value_columns"]:
        raise DisclosureError(
            f"'{value}' is not an encrypted value column of this dataset; "
            f"choose one of {sorted(ds['value_columns'])}")
    col = ds["value_columns"][value]
    ct = [_ct(pub, h) for h in col["ct"]]
    ct_sq = [_ct(pub, h) for h in col["ct_sq"]]
    mask = [_ct(pub, h) for h in col["mask"]]
    by, rows = _group_rows(ds, by)
    groups = {}
    for key, idx in rows.items():
        acc, acc_sq, acc_m = ct[idx[0]], ct_sq[idx[0]], mask[idx[0]]
        for i in idx[1:]:
            acc, acc_sq, acc_m = acc + ct[i], acc_sq + ct_sq[i], acc_m + mask[i]
        groups[str(key)] = {"key": key if isinstance(key, str) else list(key),
                            "n": len(idx),
                            "sum": _ct_hex(acc),
                            "sum_sq": _ct_hex(acc_sq),
                            "count": _ct_hex(acc_m)}
    return {"by": by, "value": value, "scale": col["scale"], "groups": groups}


class HEAuthority:
    """The release authority (spec §3): the ONLY holder of the Paillier private
    key. It gates on the plaintext unit counts FIRST and decrypts only the cells
    with n >= k, then routes the decrypted ``(table, counts)`` through the same
    backend-neutral audited suppressors as every other backend — identical
    suppression, count-noise and audit fingerprints, ``backend="paillier"``.

    v1 is the single-key authority (no maintained Python threshold-Paillier);
    the 2-of-2 threshold upgrade replaces ``self._priv`` with a share protocol
    without touching the gating or release logic (spec §6.2).
    """

    def __init__(self, private_key, policy: Policy):
        self._priv = private_key
        self._policy = policy
        self._verbs = SafeVerbs(policy)

    def _check_winsorize(self, ds: dict, value: str):
        want = self._policy.suppression.winsorize
        have = ds["value_columns"][value].get("winsorize")
        if want is not None and (have is None or
                                 [float(want[0]), float(want[1])] != [float(h) for h in have]):
            raise DisclosureError(
                f"policy demands winsorization {tuple(want)} but ciphertext cannot be "
                f"clipped at query time; the dataset must be pre-winsorized at "
                f"encryption time with the same limits (it carries: {have}).")

    def _dec(self, pub, hexct: str) -> int:
        return _decrypt_int(self._priv, _ct(pub, hexct))

    def group_agg(self, ds: dict, by, value: str, agg: str = "mean",
                  *, min_n=None, round=None) -> Released:
        """Gated grouped aggregate over an encrypted dataset. Mirrors
        ``SafeVerbs.group_agg`` semantics for the HE-computable subset."""
        if agg not in _HE_AGGS:
            raise DisclosureError(
                f"agg '{agg}' is not available on encrypted data; choose one of "
                f"{sorted(_HE_AGGS)}. Order statistics cannot be computed under "
                f"additive homomorphic encryption — use a non-encrypted plane or "
                f"pre-computed columns.")
        if value not in ds["value_columns"]:
            raise DisclosureError(
                f"'{value}' is not an encrypted value column of this dataset; "
                f"choose one of {sorted(ds['value_columns'])}")
        self._check_winsorize(ds, value)
        pub = _load_public(ds)
        blind = blind_group_agg(ds, by, value)
        scale = blind["scale"]
        gs = list(blind["groups"].values())
        keys = [g["key"] if isinstance(g["key"], str) else tuple(g["key"]) for g in gs]
        index = (pd.Index(keys, name=by) if isinstance(by, str)
                 else pd.MultiIndex.from_tuples(keys, names=list(by)))
        counts = pd.Series([g["n"] for g in gs], index=index, dtype=float).sort_index()
        # THE gate: same threshold computation as the plaintext release path —
        # cells below k are never decrypted, not merely suppressed afterwards.
        k = max(self._verbs._min_n(min_n), _agg_min_n(self._policy, agg))
        table = pd.Series(np.nan, index=counts.index, dtype=float, name=value)
        # nn_counts is the REAL suppression basis for every agg except "size":
        # the decrypted non-null mask-sum, not the raw (possibly-missing-laden)
        # unit count — mirrors the safe.py/polars_api.py group_agg fix. Starts
        # as a copy of the raw counts so cells the cheap pre-check below skips
        # (raw count already < k, so the non-null count can only be lower or
        # equal) still report a correctly-suppressing value to _release_group_agg.
        nn_counts = counts.copy()
        for key, g in zip(keys, gs):
            if agg == "size":
                # here the released value IS the raw unit count, so gating its
                # own release on that same count is correct (mirrors safe.py).
                if g["n"] < k:
                    continue
                table[key] = g["n"]
                continue
            if g["n"] < k:
                continue                       # nn <= g["n"] always: cheap pre-check,
                                                # skip decrypting an obviously-tiny cell
            nn = self._dec(pub, g["count"])    # THE real gate: non-null contributing count
            nn_counts[key] = nn
            if nn < k:
                continue                       # suppressed: sum/sum_sq never decrypted
            if agg == "count":
                table[key] = nn
                continue
            s = self._dec(pub, g["sum"]) / scale
            if agg == "sum":
                table[key] = s
                continue
            if agg == "mean":
                table[key] = s / nn if nn else np.nan
                continue
            sq = self._dec(pub, g["sum_sq"]) / (scale * scale)
            var = (sq - s * s / nn) / (nn - 1) if nn > 1 else np.nan
            table[key] = var if agg == "var" else float(np.sqrt(var))
        return self._verbs._release_group_agg(
            table, counts if agg == "size" else nn_counts, agg=agg, by=by, value=value,
            min_n=min_n, round=round, backend="paillier")

    def ols(self, ds: dict, *, y: str, x, min_n=None) -> Released:
        """Tier-1 regression (spec §3 "Regression tiers"): OLS of an *encrypted*
        outcome on *plaintext* categorical predictors.

        ``X'X`` is plain arithmetic on the dummy design; each ``X'y`` entry is a
        sum of outcome ciphertexts over one dummy level (Paillier addition), and
        RSS comes from the shipped Enc(y²) column, so the authority decrypts
        only per-level aggregates. Levels below ``min_n`` are dropped from the
        design entirely — their sums are never decrypted (the same rule as
        ``StatsMixin._numeric_design``). Missing outcomes are refused: NaN rows
        cannot be dropped blindly, so the column must be complete.
        """
        if y not in ds["value_columns"]:
            raise DisclosureError(
                f"'{y}' is not an encrypted value column of this dataset; "
                f"choose one of {sorted(ds['value_columns'])}")
        xs = [x] if isinstance(x, str) else list(x)
        for c in xs:
            if c not in ds["group_columns"]:
                raise DisclosureError(
                    f"'{c}' is not a plaintext group column of this encrypted "
                    f"dataset; predictors must be among {sorted(ds['group_columns'])}")
        self._check_winsorize(ds, y)
        pub = _load_public(ds)
        col = ds["value_columns"][y]
        scale, n = col["scale"], int(ds["n_rows"])
        k = self._verbs._min_n(min_n)

        ct = [_ct(pub, h) for h in col["ct"]]
        mask = [_ct(pub, h) for h in col["mask"]]

        # completeness gate: one full-sample aggregate (the non-null count)
        acc = mask[0]
        for m in mask[1:]:
            acc = acc + m
        if _decrypt_int(self._priv, acc) < n:
            raise DisclosureError(
                f"'{y}' has missing values; OLS on encrypted data requires a "
                f"complete outcome column (NaN rows cannot be dropped blindly). "
                f"Re-encrypt without missing values or use group_agg.")

        # plaintext design: intercept + drop-first dummies, sub-k levels dropped
        frame = pd.DataFrame({c: ds["group_columns"][c] for c in xs})
        X = pd.DataFrame({"Intercept": np.ones(n)})
        support, dropped = {"Intercept": n}, []
        for c in xs:
            dummies = pd.get_dummies(frame[c].astype(str), prefix=c, drop_first=True)
            for dcol in dummies.columns:
                cnt = int(dummies[dcol].sum())
                if cnt >= k:
                    X[dcol] = dummies[dcol].astype(float)
                    support[dcol] = cnt
                else:
                    dropped.append(str(dcol))    # never computed, never decrypted
        p = X.shape[1]
        if p < 2:
            raise DisclosureError("no predictors with sufficient support to fit a model")
        if n - p < 1:
            raise DisclosureError("too few observations for the requested design")

        # X'y: dummy columns are 0/1, so each entry is a ciphertext sum over the
        # level's rows — the only encrypted arithmetic in the whole fit.
        xty = np.empty(p)
        for j, name in enumerate(X.columns):
            rows = np.nonzero(X[name].to_numpy())[0]
            acc = ct[rows[0]]
            for i in rows[1:]:
                acc = acc + ct[i]
            xty[j] = _decrypt_int(self._priv, acc) / scale
        ct_sq = [_ct(pub, h) for h in col["ct_sq"]]
        acc = ct_sq[0]
        for c2 in ct_sq[1:]:
            acc = acc + c2
        sum_sq = _decrypt_int(self._priv, acc) / (scale * scale)

        # solve in plaintext
        from scipy import stats as _st
        xtx_inv = np.linalg.pinv(X.T.to_numpy(dtype=float) @ X.to_numpy(dtype=float))
        beta = xtx_inv @ xty
        dof = n - p
        sigma2 = max(sum_sq - beta @ xty, 0.0) / dof
        se = np.sqrt(np.clip(np.diag(sigma2 * xtx_inv), 0.0, None))
        tvals = np.divide(beta, se, out=np.zeros_like(beta), where=se > 0)
        pvals = 2 * _st.t.sf(np.abs(tvals), dof)
        tcrit = float(_st.t.ppf(0.975, dof))

        params = pd.Series(beta, index=X.columns)
        ci = pd.DataFrame({0: beta - tcrit * se, 1: beta + tcrit * se}, index=X.columns)
        res = self._verbs._release_coeffs(params, ci, pd.Series(pvals, index=X.columns),
                                          support, family="ols", n=n)
        res.audit["backend"] = "paillier"
        res.audit["terms_suppressed"] = sorted(
            set(res.audit.get("terms_suppressed", [])) | set(dropped))
        return res

    def _plain_series(self, ds: dict, col: str) -> pd.Series:
        if col not in ds["group_columns"]:
            raise DisclosureError(
                f"'{col}' is not a plaintext group column of this encrypted dataset; "
                f"frequency verbs need one of {sorted(ds['group_columns'])}")
        return pd.Series(ds["group_columns"][col], name=col)

    def value_counts(self, ds: dict, col: str, *, min_n=None, round=None) -> Released:
        """Suppressed frequency table of a plaintext group column — pure counting,
        no decryption involved; released through the shared audited suppressor."""
        counts = self._plain_series(ds, col).value_counts()
        return self._verbs._release_value_counts(counts, col=col, min_n=min_n,
                                                 round=round, backend="paillier")

    def crosstab(self, ds: dict, row: str, col: str, *, min_n=None, round=None) -> Released:
        """Suppressed cross-tabulation of two plaintext group columns."""
        tab = pd.crosstab(self._plain_series(ds, row), self._plain_series(ds, col))
        return self._verbs._release_crosstab(tab, row=row, col=col, min_n=min_n,
                                             round=round, backend="paillier")


class EncryptedSource:
    """What the server passes in ``run()``'s sources dict for ``dialect="he"``:
    the published ciphertext dataset plus the authority's private key. User code
    never sees this object — the namespace wraps it in :class:`HEFrame`."""

    def __init__(self, dataset: dict, private_key):
        if not isinstance(dataset, dict) or dataset.get("format") != FORMAT:
            raise DisclosureError(
                f"not an encrypted dataset: expected format '{FORMAT}' "
                f"(got: {dataset.get('format') if isinstance(dataset, dict) else type(dataset).__name__})")
        self.dataset = dataset
        self.private_key = private_key


# The group aggregations HE can compute (median/quantile/min/max are absent —
# additive encryption cannot order values). Mirrors SafeSeriesGroupBy's menu.
_HE_GROUP_AGGS = frozenset({"mean", "sum", "count", "size", "var", "std"})


class HESeriesGroupBy:
    """``df.groupby(by)[value]`` over an encrypted dataset — only aggregations,
    each a suppressed table. Same shape as :class:`safepy.safeframe.SafeSeriesGroupBy`
    but restricted to the homomorphically computable stats."""

    def __init__(self, ds, by, value, auth):
        self._ds, self._by, self._value, self._auth = ds, by, value, auth

    def mean(self, **kw): return self._agg("mean", **kw)
    def sum(self, **kw): return self._agg("sum", **kw)
    def count(self, **kw): return self._agg("count", **kw)
    def size(self, **kw): return self._agg("size", **kw)
    def var(self, **kw): return self._agg("var", **kw)
    def std(self, **kw): return self._agg("std", **kw)

    def __getattr__(self, name):
        # median/min/max/quantile and any other stat: refuse with the reason,
        # rather than a bare AttributeError.
        if name.startswith("_"):
            raise AttributeError(name)
        raise DisclosureError(
            f"'{name}' is not available on encrypted data; choose one of "
            f"{sorted(_HE_GROUP_AGGS)}. Order statistics (median/min/max/"
            f"quantile) cannot be computed under additive homomorphic encryption.")

    def agg(self, func, **kw):
        """``groupby(by)[value].agg('mean')`` — a single stat name only. Multiple
        stats (``['mean', 'std']``) are refused on encrypted data: unlike the
        plaintext path there is no group_agg_multi backend, so request them one
        at a time."""
        if isinstance(func, (list, tuple)):
            raise DisclosureError(
                "on encrypted data, aggregate one statistic at a time "
                "(e.g. .agg('mean')), not a list like ['mean', 'std']")
        if not isinstance(func, str):
            raise DisclosureError("agg takes a stat name like 'mean', not a function")
        return self._agg(func, **kw)

    aggregate = agg

    def _agg(self, agg, **kw):
        return self._auth.group_agg(self._ds, self._by, self._value, agg, **kw)


class HEGroupBy:
    """``df.groupby(by)`` over an encrypted dataset. Index a column for the
    pandas chaining shape ``groupby(by)[value].mean()``; the explicit-value
    shape ``groupby(by).mean(value)`` is also accepted (parity with
    :class:`safepy.safeframe.SafeGroupBy`)."""

    def __init__(self, ds, by, auth):
        self._ds, self._by, self._auth = ds, by, auth

    def _cols(self):
        return set(self._ds["group_columns"]) | set(self._ds["value_columns"])

    def __getitem__(self, value):
        if not isinstance(value, str):
            raise DisclosureError("select a single column by name, e.g. groupby(...)['salary']")
        if value not in self._cols():
            raise DisclosureError(f"{value!r} is not a column")
        return HESeriesGroupBy(self._ds, self._by, value, self._auth)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._cols():
            return HESeriesGroupBy(self._ds, self._by, name, self._auth)
        raise DisclosureError(f"{name!r} is not a column")

    # explicit-value shape: groupby(by).mean('salary')
    def mean(self, value, **kw): return self._auth.group_agg(self._ds, self._by, value, "mean", **kw)
    def sum(self, value, **kw): return self._auth.group_agg(self._ds, self._by, value, "sum", **kw)
    def count(self, value, **kw): return self._auth.group_agg(self._ds, self._by, value, "count", **kw)
    def var(self, value, **kw): return self._auth.group_agg(self._ds, self._by, value, "var", **kw)
    def std(self, value, **kw): return self._auth.group_agg(self._ds, self._by, value, "std", **kw)

    def size(self, **kw):
        # size counts rows per group; it never uses a value's ciphertext, but the
        # authority needs a valid encrypted column as the count carrier.
        value = next(iter(self._ds["value_columns"]), None)
        if value is None:
            raise DisclosureError("dataset has no encrypted value column to count over")
        return self._auth.group_agg(self._ds, self._by, value, "size", **kw)


class HEFrame:
    """Capability facade over an encrypted dataset — the only object HE scripts
    can reach. It offers the *same idiomatic surface* as the plaintext
    :class:`safepy.safeframe.SafeFrame` (``groupby``/``value_counts``/
    ``crosstab``/``ols``), just a smaller menu: no median, no filters or derived
    columns on encrypted values. So a user writes ordinary pandas and only meets
    a clear refusal when an operation is not homomorphically computable — they do
    not learn a new vocabulary. Every method routes through the policy-bound
    :class:`HEAuthority`; the gate blocks ``_``-private attributes, so the raw
    ciphertext and key are unreachable from user code."""

    _is_heframe = True

    def __init__(self, source: EncryptedSource, authority: HEAuthority):
        self._ds = source.dataset
        self._auth = authority

    def groupby(self, by) -> HEGroupBy:
        """``df.groupby('region')`` / ``df.groupby(['region', 'sex'])`` — the
        idiomatic entry point; chain a column and a stat."""
        return HEGroupBy(self._ds, by, self._auth)

    def value_counts(self, col: str, *, min_n=None, round=None) -> Released:
        return self._auth.value_counts(self._ds, col, min_n=min_n, round=round)

    def crosstab(self, row: str, col: str, *, min_n=None, round=None) -> Released:
        return self._auth.crosstab(self._ds, row, col, min_n=min_n, round=round)

    def ols(self, *, y: str, x, min_n=None) -> Released:
        return self._auth.ols(self._ds, y=y, x=x, min_n=min_n)

    def group_agg(self, by, value: str, agg: str = "mean", *, min_n=None, round=None) -> Released:
        """Explicit verb form, kept as an alias of the idiomatic
        ``groupby(by)[value].agg(agg)`` (and of safepy's internal verb name)."""
        return self._auth.group_agg(self._ds, by, value, agg, min_n=min_n, round=round)


def build_he_namespace(sources: dict, policy: Policy) -> dict:
    """Namespace for ``dialect="he"``: each source becomes an HEFrame bound to
    its own authority (each dataset has its own key). No ``pd``/``np``/``safe`` —
    the facade methods are the entire reachable surface."""
    ns = {}
    for name, src in sources.items():
        if not isinstance(src, EncryptedSource):
            raise DisclosureError(
                f"source '{name}' is not an EncryptedSource; the 'he' dialect "
                f"only accepts encrypted datasets (wrap with he.EncryptedSource)")
        ns[name] = HEFrame(src, HEAuthority(src.private_key, policy))
    return ns
