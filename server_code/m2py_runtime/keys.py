# ============================================================================
# GENERATED COPY — DO NOT EDIT HERE.
# Source of truth: the m2py repo. This file is produced by sync_to_api.py.
# Edit the engine in the m2py repo and re-run that script; direct edits here
# are overwritten on the next sync.
# ============================================================================
"""Shared merge-key resolution — single source of truth.

Both the emulator (``m2py.MicroInterpreter`` merge handler) and the offline
translator (``m2py_translate``) resolve a ``merge``'s join key through this
module, so the offline script joins on exactly the column the emulator would.

This module is dependency-free (no import of ``m2py``) so the translator can
call it at translation time from only column *names* — it never needs live
DataFrames or the heavy emulator.

Semantics mirror the emulator's into-form merge (``merge vars into TARGET``):
the LEFT side of the resulting pandas merge is the *target* (the ``into``
dataset) and the RIGHT side is the *source* (the active dataset). Therefore
``left_on`` indexes target columns and ``right_on`` indexes source columns.
"""

from collections import namedtuple

# Entity key columns, in the same precedence order as m2py._get_df_key_col.
ENTITY_KEY_COLS = (
    "PERSONID_1", "ARBEIDSFORHOLD_ID", "KJORETOY_ID",
    "NUDB_KURS_LOEPENR", "AGGRSHOPPID", "NPRID", "unit_id",
)

# Resolution result. ``left_on``/``right_on`` are the join columns (target side /
# source side). ``status`` is "ok" or "error". ``reason`` is None on success, or
# one of the codes below so the emulator can reproduce its exact message.
# ``src_key``/``tgt_key`` are the resolved entity/collapse keys (the emulator's
# messages reference them).
MergeKeyResolution = namedtuple(
    "MergeKeyResolution",
    ["left_on", "right_on", "status", "reason", "src_key", "tgt_key"],
)

# reason codes (status == "error")
ON_VAR_ONLY_IN_TARGET_NO_SRC_KEY = "on_var_only_in_target_no_src_key"
ON_VAR_IN_NEITHER = "on_var_in_neither"
NO_COMMON_KEY = "no_common_key"


def key_col_from_cols(cols):
    """Return the entity key column present in ``cols``, or None.

    Column-list analogue of ``m2py._get_df_key_col`` (which takes a DataFrame).
    """
    cols = list(cols)
    for c in ENTITY_KEY_COLS:
        if c in cols:
            return c
    return None


def resolve_merge_key(source_cols, target_cols, on_var=None,
                      src_collapse_key=None, tgt_collapse_key=None,
                      is_person_ref=None):
    """Resolve the join key for ``merge <vars> into TARGET [on on_var]``.

    Parameters mirror the emulator's merge handler state:
      - ``source_cols``: columns of the active (source) dataset.
      - ``target_cols``: columns of the ``into`` (target) dataset.
      - ``on_var``: the explicit ``on`` variable, or None.
      - ``src_collapse_key`` / ``tgt_collapse_key``: the tracked key column set
        by a prior ``collapse``/``aggregate`` (``dataset_key_cols`` in the
        emulator), used for the FNR person-ref cross-entity linkage.
      - ``is_person_ref``: callable ``alias -> bool`` telling whether a column is
        a person-reference FNR variable (the emulator passes a closure over its
        catalog; the translator passes one over the same catalog). None disables
        the cross-entity linkage step.

    Returns a ``MergeKeyResolution`` namedtuple ``(left_on, right_on, status,
    reason, src_key, tgt_key)``. ``status`` is ``"ok"`` or ``"error"``;
    ``left_on`` indexes target columns, ``right_on`` indexes source columns. On
    ``"error"`` a best-guess ``(left_on, right_on)`` (the source key on both
    sides) is still returned so the translator can bake-and-flag; the emulator
    instead formats ``reason`` into its message and aborts.
    """
    source_cols = list(source_cols)
    target_cols = list(target_cols)
    if is_person_ref is None:
        is_person_ref = lambda alias: False  # noqa: E731

    # src_key: entity key, else tracked collapse key; must be a real source col;
    # final fallback is the first source column (or 'unit_id').
    src_key = key_col_from_cols(source_cols) or src_collapse_key
    if src_key and src_key not in source_cols:
        src_key = None
    if src_key is None:
        src_key = source_cols[0] if source_cols else "unit_id"
    tgt_key = key_col_from_cols(target_cols) or "unit_id"

    def ok(left, right):
        return MergeKeyResolution(left, right, "ok", None, src_key, tgt_key)

    def err(reason):
        return MergeKeyResolution(src_key, src_key, "error", reason,
                                  src_key, tgt_key)

    if on_var:
        in_src = on_var in source_cols
        in_tgt = on_var in target_cols
        if in_src and in_tgt:
            return ok(on_var, on_var)
        if in_tgt:
            # on_var only in target -> join target.on_var to source's key
            if src_key not in source_cols:
                return err(ON_VAR_ONLY_IN_TARGET_NO_SRC_KEY)
            return ok(on_var, src_key)
        if in_src:
            # target.tgt_key == source.on_var
            return ok(tgt_key, on_var)
        return err(ON_VAR_IN_NEITHER)

    # No explicit on_var.
    if src_key in target_cols:
        return ok(src_key, src_key)
    if tgt_key in source_cols:
        return ok(tgt_key, tgt_key)

    common = list(set(source_cols) & set(target_cols))
    if common:
        return ok(common[0], common[0])

    # FNR person-ref cross-entity linkage (e.g. mother-FNR collapse key vs
    # PERSONID_1 on the other side).
    if (src_collapse_key and src_collapse_key in source_cols
            and is_person_ref(src_collapse_key)):
        pid = key_col_from_cols(target_cols)
        if pid and pid in target_cols:
            return ok(pid, src_collapse_key)
    if (tgt_collapse_key and tgt_collapse_key in target_cols
            and is_person_ref(tgt_collapse_key)):
        pid = key_col_from_cols(source_cols)
        if pid and pid in source_cols:
            return ok(tgt_collapse_key, pid)

    return err(NO_COMMON_KEY)
