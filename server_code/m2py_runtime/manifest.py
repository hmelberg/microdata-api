# ============================================================================
# GENERATED COPY — DO NOT EDIT HERE.
# Source of truth: the m2py repo. This file is produced by sync_to_api.py.
# Edit the engine in the m2py repo and re-run that script; direct edits here
# are overwritten on the next sync.
# ============================================================================
"""Source manifest — the generalized, source-agnostic catalog.

Carries, per logical dataset, where it lives, its format, its key(s), entity,
sensitivity, and optional variable metadata. The non-sensitive schema the
browser/translator consumes; physical resolution + secrets stay server-side.
"""


def _format_from(location, explicit=None):
    """Format from explicit field, else inferred from the source extension."""
    if explicit:
        return explicit
    loc = location.lower()
    for ext, fmt in (
        (".parquet", "parquet"), (".csv", "csv"),
        (".duckdb", "duckdb"), (".db", "duckdb"),
        (".sqlite", "sql"), (".json", "manifest"),
    ):
        if loc.endswith(ext):
            return fmt
    raise ValueError(f"cannot infer format for source {location!r}")


class Manifest:
    """Read-only view over a manifest dict (see the plan's contract shape)."""

    def __init__(self, datasets):
        self._d = datasets

    @classmethod
    def from_dict(cls, d):
        return cls(dict((d or {}).get("datasets") or {}))

    def names(self):
        return list(self._d)

    def has(self, name):
        return name in self._d

    def location(self, name):
        return self._d[name]["source"]

    def format(self, name):
        e = self._d[name]
        return _format_from(e["source"], e.get("format"))

    def keys(self, name):
        return list(self._d[name].get("keys") or [])

    def entity(self, name):
        return self._d[name].get("entity")

    def is_sensitive(self, name):
        return bool(self._d[name].get("sensitive", False))

    def variables(self, name):
        return dict(self._d[name].get("variables") or {})
