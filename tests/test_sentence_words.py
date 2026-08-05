"""Setningskoder: entropivakter (3×256 unike ord → 40 bits over 5 slott) og
format-kontrakt mot normalisereren i auth_hash."""
import random
import re

import auth_hash
import sentence_words


def test_listene_har_256_unike_gyldige_ord():
    for lst in (sentence_words.ADJECTIVES, sentence_words.NOUNS, sentence_words.VERBS):
        assert len(lst) == 256
        assert len(set(lst)) == 256
        for w in lst:
            assert re.match(r"^[a-z]{3,9}$", w), w


def test_generator_form_og_normalisering():
    rng = random.Random(42)
    code = sentence_words.generate_sentence_code(rng)
    parts = code.split("-")
    assert len(parts) == 5
    assert parts[0] in sentence_words.ADJECTIVES
    assert parts[1] in sentence_words.NOUNS
    assert parts[2] in sentence_words.VERBS
    assert parts[3] in sentence_words.ADJECTIVES
    assert parts[4] in sentence_words.NOUNS
    # Kanonisk form overlever normalisering; mellomrom-form er likeverdig.
    assert auth_hash.normalize_code(code) == code
    assert auth_hash.normalize_code(code.replace("-", " ").upper()) == code
    assert auth_hash.hash_code(code) == auth_hash.hash_code(code.replace("-", " "))
