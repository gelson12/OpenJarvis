"""Tests for the shared NLU parsers."""

from __future__ import annotations

from datetime import date, timedelta

from accommodation.nlu import (
    parse_dates,
    parse_guests,
    parse_location,
    parse_provider_preference,
)


# Fixed "today" so tests are deterministic. 2026-06-01 is a Monday.
TODAY = date(2026, 6, 1)


# ── Location ─────────────────────────────────────────────────────────


def test_location_in_phrase():
    assert parse_location("find me a hotel in Lisbon for the weekend") == "Lisbon"


def test_location_near_phrase():
    assert parse_location("somewhere near Heathrow tomorrow night") == "Heathrow"


def test_location_multi_word():
    assert (
        parse_location("book a place in New York for 3 nights")
        in ("New York", "New York City")  # accept either truncation
    )


def test_location_empty_when_missing():
    assert parse_location("find me a hotel for the weekend") == ""


# ── Regression: bug from live voice test 2026-05-27 — the parser was
#    grabbing "to <verb> ... in <place>" greedily and returning the verb
#    phrase as the location ("Rent In Lisbon" instead of "Lisbon"). The
#    two-pass parser drops `to` from primary anchors so this can't recur.


def test_location_houses_to_rent():
    assert parse_location("houses to rent in Lisbon") == "Lisbon"


def test_location_show_me_houses_to_rent():
    assert parse_location("show me all the available houses to rent in Lisbon") == "Lisbon"


# ── Regression: bug from live voice test 2026-05-29 ──────────────────────
#    STT doubled the preposition ("in in Lisbon") so the parser captured
#    "in Lisbon" and the widget showed "Stays in In Lisbon". And the
#    follow-up "in any of your sources" was parsed as a city called
#    "Any Of Your Sources". The kernel-grade parser strips leading filler
#    and rejects non-place captures.


def test_location_doubled_preposition():
    assert parse_location("any accommodation to rent in in Lisbon for this weekend") == "Lisbon"


def test_location_triple_preposition():
    assert parse_location("in in in Lisbon") == "Lisbon"


def test_location_any_of_your_sources_is_not_a_place():
    assert parse_location("any other sources, is there no houses to rent or to let?") == ""
    assert parse_location("no place in any of your sources") == ""
    assert parse_location("isn't there anything in any other sources") == ""


def test_location_spend_the_weekend():
    assert parse_location("I need to spend the weekend in Lisbon") == "Lisbon"


def test_location_trailing_fragment_picks_last_match():
    """When a multi-utterance turn mentions two cities, pick the last one."""
    txt = "I want a weekend in Lisbon. Actually, change that — in Porto."
    assert parse_location(txt) == "Porto"


def test_location_trip_to_fallback():
    """No primary `in/at/near/around` — fall back to `trip/going to <Place>`."""
    assert parse_location("trip to Rome") == "Rome"
    assert parse_location("going to Paris next week") == "Paris"
    assert parse_location("visiting Berlin for a couple of days") == ""  # no anchor verb


def test_location_long_user_transcript():
    """Real failing transcript from the 2026-05-27 voice session."""
    txt = (
        "can you show me all the availabilities of I need to spend the "
        "weekend in Lisbon. Show me all the available houses to rent. "
        "In Lisbon."
    )
    assert parse_location(txt) == "Lisbon"


# ── Guests ───────────────────────────────────────────────────────────


def test_guests_defaults_to_two():
    assert parse_guests("find me a hotel in Lisbon") == 2


def test_guests_solo():
    assert parse_guests("just me, find a room in Berlin") == 1


def test_guests_couple_phrase():
    assert parse_guests("hotel in Paris for me and my partner") == 2


def test_guests_family_of_four():
    assert parse_guests("family of 4 in Lisbon") == 4


def test_guests_three_adults():
    assert parse_guests("place for 3 adults in Bath") == 3


def test_guests_clamped():
    # Bogus large numbers get clamped to 16.
    assert parse_guests("for 99 people") == 16


# ── Provider preference ──────────────────────────────────────────────


def test_provider_pref_airbnb():
    assert parse_provider_preference("find me an Airbnb in Lisbon") == ["apify_airbnb"]


def test_provider_pref_hotel():
    assert parse_provider_preference("find me a hotel in Lisbon") == ["liteapi"]


def test_provider_pref_empty_when_both():
    # Both keywords → fan out (no preference enforced).
    assert parse_provider_preference("hotel or airbnb in Lisbon") == []


def test_provider_pref_empty_when_neither():
    assert parse_provider_preference("where to stay in Lisbon") == []


# ── Dates ────────────────────────────────────────────────────────────


def test_dates_weekend_from_monday():
    ci, co = parse_dates("weekend in Lisbon", today=TODAY)  # Mon → Fri = 4 days
    assert ci == TODAY + timedelta(days=4)
    assert (co - ci).days == 2


def test_dates_tonight():
    ci, co = parse_dates("hotel tonight", today=TODAY)
    assert ci == TODAY
    assert co == TODAY + timedelta(days=1)


def test_dates_tomorrow():
    ci, co = parse_dates("hotel tomorrow", today=TODAY)
    assert ci == TODAY + timedelta(days=1)


def test_dates_for_three_nights():
    ci, co = parse_dates("place for 3 nights", today=TODAY)
    assert (co - ci).days == 3


def test_dates_for_a_week():
    ci, co = parse_dates("hotel for a week", today=TODAY)
    assert (co - ci).days == 7


def test_dates_named_weekday():
    # "next friday" → 4 days from Monday (4 - 0 = 4).
    ci, co = parse_dates("hotel from friday", today=TODAY)
    assert ci.weekday() == 4
    assert ci >= TODAY


def test_dates_explicit_range_same_month():
    ci, co = parse_dates("from June 5 to June 8 in Lisbon", today=TODAY)
    assert ci == date(2026, 6, 5)
    assert co == date(2026, 6, 8)


def test_dates_explicit_range_short_form():
    ci, co = parse_dates("June 5 to 8 in Lisbon", today=TODAY)
    assert ci == date(2026, 6, 5)
    assert co == date(2026, 6, 8)


def test_dates_fallback_when_nothing_matches():
    ci, co = parse_dates("find me a hotel", today=TODAY)
    assert ci == TODAY + timedelta(days=7)
    assert (co - ci).days == 2


def test_dates_explicit_range_pushes_year_when_past():
    # "January 5 to January 10" when today is June 1 → next year's January.
    ci, co = parse_dates("from January 5 to January 10", today=TODAY)
    assert ci == date(2027, 1, 5)
    assert co == date(2027, 1, 10)
