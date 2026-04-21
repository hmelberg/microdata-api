"""BM25 retrieval + synonym expansion + lookup_variable tool backend.

Reads its corpus from Anvil Data Files (not Data Tables):

    anvil_app Data Files:
      - corpus.pkl     — pickled dict with every static table + pre-
                         tokenised BM25 corpora. Produced by
                         seed/build_data_files.py.
      - synonyms.json  — hand-authored EN↔NO domain synonyms.

At server-module import the files are loaded once; BM25Okapi instances are
constructed from the pre-tokenised corpora (fast — no stemming at startup).
Memory footprint is modest (~10-30 MB for the whole corpus).

Call `reload_data_files()` after uploading a new corpus.pkl to pick up
changes without restarting the worker.
"""

from __future__ import annotations

import json
import pickle
import re
import unicodedata
from dataclasses import dataclass

import anvil.server
from anvil.files import data_files
from rank_bm25 import BM25Okapi

try:
    import snowballstemmer
    _NO_STEMMER = snowballstemmer.stemmer("norwegian")
    _EN_STEMMER = snowballstemmer.stemmer("english")
except ImportError:  # pragma: no cover
    _NO_STEMMER = None
    _EN_STEMMER = None


_TOKEN_RE = re.compile(r"[a-zA-ZæøåÆØÅ0-9]{2,}", re.UNICODE)


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text.lower())


def tokenize(text: str) -> list[str]:
    """Must match seed/build_data_files.py::tokenize exactly."""
    raw = _TOKEN_RE.findall(_fold(text))
    if not raw:
        return []
    if _NO_STEMMER is None:
        return raw
    return _NO_STEMMER.stemWords(raw) + _EN_STEMMER.stemWords(raw)


# ---------------------------------------------------------------------------
# Module-level state


@dataclass
class _Index:
    bm25: BM25Okapi | None
    docs: list[dict]


_corpus: dict | None = None
_variables_index: _Index = _Index(bm25=None, docs=[])
_examples_index: _Index = _Index(bm25=None, docs=[])
_manual_index: _Index = _Index(bm25=None, docs=[])
_synonyms_cache: dict[str, list[str]] = {}
_variable_names: set[str] = set()
_command_names: set[str] = set()
_commands_by_name: dict[str, dict] = {}
_examples_by_ext_id: dict[str, dict] = {}


def _load_corpus() -> None:
    global _corpus, _variables_index, _examples_index, _manual_index
    global _synonyms_cache, _variable_names, _command_names
    global _commands_by_name, _examples_by_ext_id

    with open(data_files["corpus.pkl"], "rb") as f:
        _corpus = pickle.load(f)

    bm25 = _corpus["bm25"]
    _variables_index = _Index(
        bm25=BM25Okapi(bm25["variables"]) if bm25["variables"] else None,
        docs=_corpus["variables"],
    )
    _examples_index = _Index(
        bm25=BM25Okapi(bm25["examples"]) if bm25["examples"] else None,
        docs=_corpus["examples"],
    )
    _manual_index = _Index(
        bm25=BM25Okapi(bm25["manual_sections"]) if bm25["manual_sections"] else None,
        docs=_corpus["manual_sections"],
    )
    _variable_names = set(_corpus.get("variable_names") or [])
    _command_names = set(_corpus.get("command_names") or [])
    _commands_by_name = {c["name"]: c for c in _corpus.get("commands") or []}
    _examples_by_ext_id = {e["ext_id"]: e for e in _corpus.get("examples") or []}

    # Synonyms
    with open(data_files["synonyms.json"], "r", encoding="utf-8") as f:
        rows = json.load(f)
    syn: dict[str, list[str]] = {}
    for row in rows:
        term = (row.get("term") or "").lower()
        syns = row.get("synonyms") or []
        if term and syns:
            syn.setdefault(term, []).extend(syns)
    _synonyms_cache = syn


def _ensure_loaded() -> None:
    if _corpus is None:
        _load_corpus()


@anvil.server.callable
def reload_data_files() -> dict:
    """Re-read corpus.pkl + synonyms.json after a fresh upload.

    Bust the cached prompt prefix so any new content (top_variables,
    cheat sheets) takes effect on the next request.
    """
    _load_corpus()
    try:
        import prompts
        prompts.refresh_cached_prefix()
    except Exception:
        pass
    return {
        "variables": len(_variables_index.docs),
        "examples": len(_examples_index.docs),
        "manual_sections": len(_manual_index.docs),
        "commands": len(_command_names),
        "synonyms": len(_synonyms_cache),
        "top_variables": len((_corpus or {}).get("top_variables") or []),
        "schema_version": (_corpus or {}).get("schema_version", 1),
    }


# ---------------------------------------------------------------------------
# Public accessors (used by validation/prompts)


def get_corpus() -> dict:
    _ensure_loaded()
    assert _corpus is not None
    return _corpus


def known_commands() -> set[str]:
    _ensure_loaded()
    return _command_names


def known_variables() -> set[str]:
    _ensure_loaded()
    return _variable_names


def commands_by_name() -> dict[str, dict]:
    _ensure_loaded()
    return _commands_by_name


def examples_by_ext_id() -> dict[str, dict]:
    _ensure_loaded()
    return _examples_by_ext_id


# ---------------------------------------------------------------------------
# Query helpers


def expand_synonyms(query: str) -> list[str]:
    _ensure_loaded()
    tokens = _TOKEN_RE.findall(_fold(query))
    expanded: list[str] = list(tokens)
    for tok in tokens:
        for syn in _synonyms_cache.get(tok, []):
            expanded.append(syn.lower())
    return expanded


def _bm25_top_k(idx: _Index, query_tokens: list[str], k: int) -> list[tuple[float, dict]]:
    if not idx.bm25 or not query_tokens:
        return []
    scores = idx.bm25.get_scores(query_tokens)
    ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
    out: list[tuple[float, dict]] = []
    for i, s in ranked[:k]:
        if s <= 0:
            break
        out.append((float(s), idx.docs[i]))
    return out


def search_variables(query: str, lang: str = "no", k: int = 15) -> list[dict]:
    _ensure_loaded()
    tokens = tokenize(" ".join(expand_synonyms(query)))
    candidates = _bm25_top_k(_variables_index, tokens, k=k * 2)

    raw = (query or "").upper()
    boosted: list[tuple[float, dict]] = []
    seen: set[str] = set()
    if raw and len(raw) >= 3:
        for sc, doc in candidates:
            if raw in doc["name"]:
                boosted.append((sc + 1000.0, doc))
                seen.add(doc["name"])
    for sc, doc in candidates:
        if doc["name"] not in seen:
            boosted.append((sc, doc))
    boosted.sort(key=lambda x: x[0], reverse=True)
    return [doc for _, doc in boosted[:k]]


def search_examples(query: str, lang: str = "no", k: int = 5,
                    boost_commands: list[str] | None = None) -> list[dict]:
    _ensure_loaded()
    tokens = tokenize(" ".join(expand_synonyms(query)))
    candidates = _bm25_top_k(_examples_index, tokens, k=k * 3)
    boost = set(boost_commands or [])
    rescored: list[tuple[float, dict]] = []
    for sc, doc in candidates:
        overlap = len(boost & set(doc.get("commands_used") or []))
        rescored.append((sc + overlap * 0.5, doc))
    rescored.sort(key=lambda x: x[0], reverse=True)
    return [doc for _, doc in rescored[:k]]


def search_manual(query: str, lang: str = "no", k: int = 3) -> list[dict]:
    _ensure_loaded()
    tokens = tokenize(" ".join(expand_synonyms(query)))
    return [doc for _, doc in _bm25_top_k(_manual_index, tokens, k=k)]


# ---------------------------------------------------------------------------
# Tool-use backend


@anvil.server.callable
def lookup_variable(query: str, lang: str = "no", k: int = 8) -> list[dict]:
    hits = search_variables(query=query, lang=lang, k=k)
    return [
        {
            "name": h["name"],
            "short_title": h.get("short_title", ""),
            "description": h.get("description", ""),
            "data_type": h.get("data_type", ""),
            "temporalitet": h.get("temporalitet", ""),
            "enhetstype": h.get("enhetstype", ""),
        }
        for h in hits
    ]


@anvil.server.callable
def server_variable_search(query: str, lang: str = "no", k: int = 15) -> list[dict]:
    hits = search_variables(query=query, lang=lang, k=k)
    return [
        {
            "name": h["name"],
            "short_title": h.get("short_title", ""),
            "description": h.get("description", ""),
            "data_type": h.get("data_type", ""),
            "temporalitet": h.get("temporalitet", ""),
            "enhetstype": h.get("enhetstype", ""),
            "keywords": h.get("keywords", []),
        }
        for h in hits
    ]
