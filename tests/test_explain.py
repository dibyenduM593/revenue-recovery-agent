from app.explain import verify_numbers

BUNDLE = {"verifiable_numbers": ["322", "2,476", "5.8", "+15.1", "1", "20%"]}


def test_narrative_using_only_allowed_numbers_verifies():
    verified, unverified = verify_numbers("322 is at risk on attempt #1, a lift of +15.1pp.", BUNDLE)
    assert verified
    assert unverified == []


def test_number_not_in_bundle_is_flagged():
    verified, unverified = verify_numbers("999 is at risk.", BUNDLE)
    assert not verified
    assert unverified == ["999"]


def test_digits_embedded_in_an_entity_id_are_not_flagged():
    # This is the actual bug caught during verification: a naive
    # scanning regex pulled "52816" out of "6f52816a" and flagged it as an
    # unverifiable financial figure. Whole-word tokenization must ignore it.
    verified, unverified = verify_numbers("Invoice 6f52816a: 322 at risk.", BUNDLE)
    assert verified
    assert unverified == []


def test_comma_grouped_number_matches_its_bundle_entry():
    verified, unverified = verify_numbers("Payment has 2,476 at risk.", BUNDLE)
    assert verified


def test_percentage_matches_with_or_without_percent_sign_in_bundle():
    verified, unverified = verify_numbers("Recovery rate was 5.8%.", BUNDLE)
    assert verified


def test_mixed_verified_and_unverified_flags_only_the_bad_one():
    verified, unverified = verify_numbers("322 recovered, but 500 more were expected.", BUNDLE)
    assert not verified
    assert unverified == ["500"]
