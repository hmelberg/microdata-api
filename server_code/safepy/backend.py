# ============================================================================
# GENERATED COPY — DO NOT EDIT HERE.
# Source of truth: the safepy repo. This file is produced by sync_to_api.py.
# Edit the engine in the safepy repo and re-run that script; direct edits here
# are overwritten on the next sync.
# ============================================================================
"""The common verb interface shared by every release backend.

safepy separates the *surface* a user writes (pandas facade, translated R,
gated SQL) from the *release core* that suppresses and audits results. Between
them sits a small verb interface: the operations that (a) a language front-end
knows how to target and (b) more than one backend can compute. Two backends
implement it today:

- :class:`safepy.safe.SafeVerbs` — computes on a plaintext pandas DataFrame.
- :class:`safepy.he.HEAuthority` — computes on a Paillier-encrypted dataset,
  decrypting only the cells that pass suppression.

Because both satisfy the same signatures, a dialect front-end that targets this
interface is *backend-agnostic*: inject ``SafeVerbs`` to analyse a plaintext
frame, or ``HEAuthority`` to analyse encrypted data online — the same R
translator or SQL mapper serves both. The leading ``data`` argument is the
backend's data handle (a DataFrame for ``SafeVerbs``, an encrypted-dataset dict
for ``HEAuthority``); everything downstream (min-n suppression, count-noise,
audit fingerprints) is the single shared release core in :mod:`safepy.safe`.

This interface is deliberately the *homomorphically computable subset*.
``SafeVerbs`` also offers ``median``, ``pivot_table``, ``group_agg_multi`` and
the full model family (logit/poisson/cox/…); those are **not** part of the
contract because additive homomorphic encryption cannot compute them. A front
end targeting the encrypted path must restrict itself to these four verbs.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .result import Released


@runtime_checkable
class ReleaseBackend(Protocol):
    """The four verbs every release backend must provide (see module docstring).

    ``data`` is the backend's data handle. Aggregates accept a stricter
    ``min_n`` and a rounding base; the backend may only make suppression
    *stricter* than its policy floor, never weaker.
    """

    def group_agg(self, data: Any, by, value: str, agg: str = "mean",
                  *, min_n=None, round=None) -> Released: ...

    def value_counts(self, data: Any, col: str,
                     *, min_n=None, round=None) -> Released: ...

    def crosstab(self, data: Any, row: str, col: str,
                 *, min_n=None, round=None) -> Released: ...

    def ols(self, data: Any, *, y: str, x, min_n=None) -> Released: ...
