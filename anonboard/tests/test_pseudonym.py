from anonboard.pseudonym import ADJECTIVES, ANIMALS, MAX_DERIVATION_TRIES, derive_alias


def test_wordlists_are_unique():
    assert len(set(ADJECTIVES)) == len(ADJECTIVES)
    assert len(set(ANIMALS)) == len(ANIMALS)


def test_same_device_same_thread_is_stable():
    assert derive_alias(b"pepper", "t1", "d1") == derive_alias(b"pepper", "t1", "d1")


def test_same_device_other_thread_differs_for_most_threads():
    base = derive_alias(b"pepper", "t0", "d1")
    others = [derive_alias(b"pepper", f"t{i}", "d1") for i in range(1, 200)]
    # 6400 name combinations: a handful of accidental repeats is expected,
    # anything close to systematic reuse is a bug.
    assert sum(o[0] == base[0] for o in others) < 5


def test_other_device_same_thread_differs_for_most_devices():
    base = derive_alias(b"pepper", "t1", "d0")
    others = [derive_alias(b"pepper", "t1", f"d{i}") for i in range(1, 200)]
    assert sum(o[0] == base[0] for o in others) < 5


def test_pepper_changes_everything():
    assert derive_alias(b"a", "t1", "d1") != derive_alias(b"b", "t1", "d1")


def test_counter_gives_new_candidates_and_numbered_fallback():
    names = {derive_alias(b"p", "t", "d", c)[0] for c in range(MAX_DERIVATION_TRIES)}
    assert len(names) > MAX_DERIVATION_TRIES // 2
    assert "#" in derive_alias(b"p", "t", "d", MAX_DERIVATION_TRIES)[0]


def test_empty_pepper_refused():
    import pytest

    with pytest.raises(ValueError):
        derive_alias(b"", "t", "d")
