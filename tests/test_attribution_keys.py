import uuid

from app.canonical.attribution_keys import internal_id_from, make_key, provider_id_from, split_key

INTERNAL = uuid.UUID("00103cd1-3c65-502c-b795-844ba69a6956")
PROVIDER = "order_esDHleaMWUreZr"


def test_round_trip_carries_both_halves():
    key = make_key(INTERNAL, PROVIDER)
    assert split_key(key) == (str(INTERNAL), PROVIDER)


def test_provider_half_is_what_attribution_matches_on():
    """The whole point: a reader asking for the provider id must never get
    handed the internal UUID. That substitution is what silently recorded
    11,609 payments as not-recovered."""
    key = make_key(INTERNAL, PROVIDER)
    assert provider_id_from(key) == PROVIDER
    assert provider_id_from(key) != str(INTERNAL)


def test_internal_half_still_recoverable():
    key = make_key(INTERNAL, PROVIDER)
    assert internal_id_from(key) == str(INTERNAL)


def test_bare_internal_id_still_parses():
    """Rows written before the key became composite: provider is None, and
    callers fall back to a database lookup rather than crashing."""
    assert split_key(str(INTERNAL)) == (str(INTERNAL), None)
    assert provider_id_from(str(INTERNAL)) is None


def test_missing_provider_id_degrades_to_bare_internal():
    assert make_key(INTERNAL, None) == str(INTERNAL)
    assert make_key(INTERNAL, "") == str(INTERNAL)


def test_empty_and_none_are_safe():
    assert split_key(None) == (None, None)
    assert split_key("") == (None, None)


def test_separator_absent_from_both_id_formats():
    """The format only works because neither half can contain the separator."""
    from app.canonical.attribution_keys import SEPARATOR

    assert SEPARATOR not in str(INTERNAL)
    assert SEPARATOR not in PROVIDER
