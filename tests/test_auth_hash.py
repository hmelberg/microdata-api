"""Kodehash-splitten (askstat konto-runden fase 2): magic-/delt-koder hashes
med PBKDF2-600k + fast salt (DB-dump-vern for 3-ords koder); sesjonstokens
beholder sha256 i auth.py. normalize_code speiles i askstats
js/keys-crypto.js — vektorene her er kontrakten."""
import hashlib

import auth_hash


def test_normalize_vektorer():
    assert auth_hash.normalize_code("Abacus Charity Twelve") == "abacus-charity-twelve"
    assert auth_hash.normalize_code("abacus-charity-twelve") == "abacus-charity-twelve"
    assert auth_hash.normalize_code("  aB2c__dE  ") == "ab-c-de"
    assert auth_hash.normalize_code("---") == ""
    assert auth_hash.normalize_code("") == ""


def test_hash_er_pbkdf2_av_normalisert():
    fasit = hashlib.pbkdf2_hmac(
        "sha256", b"abacus-charity-twelve", b"mdataapi-code-salt-v1", 600_000
    ).hex()
    assert auth_hash.hash_code("abacus-charity-twelve") == fasit
    assert auth_hash.hash_code("Abacus Charity Twelve") == fasit  # normalisert-invariant
    assert len(fasit) == 64


def test_hash_er_ulik_sha256():
    raw = "abacus-charity-twelve"
    assert auth_hash.hash_code(raw) != hashlib.sha256(raw.encode()).hexdigest()
