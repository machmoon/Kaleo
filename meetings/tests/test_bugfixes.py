"""Regressions for three bugs found by review, not by the tests that shipped.

Kept in their own file because of what they have in common: every one of them
failed *silently*. None raised, none logged, and the two in `_parse_rfc3339`
were found only because a reviewer enumerated timestamp shapes rather than
trusting the one shape Google happens to send today.

The worst of the three did not fail at all -- it returned a confidently wrong
answer. That is the class this repo cares most about, and it is why these
assertions check values rather than merely checking that nothing raised.
"""

from __future__ import annotations

import pytest

from meetings.config import ConfigError, MeetConfig
from meetings.meet import _parse_rfc3339


class TestTimestampsWithAnOffsetFullOfDigits:
    """The fraction was split by filtering digits instead of by position.

    An offset like ``+02:00`` is mostly digits, so the old code pulled them
    into the fractional seconds. Two failures came out of one mistake.
    """

    def test_a_millisecond_timestamp_parses_at_all(self):
        """Three fractional digits used to return None.

        None means "undateable" to ``recent_conferences``, which skips the
        conference -- so a meeting would have been dropped without a word.
        """
        parsed = _parse_rfc3339("2026-08-31T10:00:00.123Z")
        assert parsed is not None
        assert parsed.microsecond == 123000

    def test_an_offset_is_not_eaten_into_the_fraction(self):
        """The bad one: a wrong time, reported as a good one.

        ``10:00:00.5+02:00`` parsed as ``.502000+00:00`` -- the offset's digits
        became fractional seconds and the timezone was lost, putting the
        timestamp two hours out. Nothing downstream could tell.
        """
        parsed = _parse_rfc3339("2026-08-31T10:00:00.5+02:00")
        assert parsed is not None
        assert parsed.microsecond == 500000, "offset digits leaked into the fraction"
        assert parsed.utcoffset().total_seconds() == 2 * 3600, "offset was dropped"

    def test_nanoseconds_still_truncate_to_microseconds(self):
        """The shape Google actually sends must keep working."""
        parsed = _parse_rfc3339("2026-08-31T10:00:00.123456789Z")
        assert parsed is not None
        assert parsed.microsecond == 123456

    @pytest.mark.parametrize("value", ["", "garbage", "2026-13-45T99:99:99Z"])
    def test_an_unparseable_timestamp_is_none_rather_than_an_exception(self, value):
        """A timestamp we cannot read must not abandon the whole poll."""
        assert _parse_rfc3339(value) is None


class TestNumericConfigRaisesConfigError:
    """A typo'd env var escaped as ValueError, breaking the module's contract.

    Every other misconfiguration here surfaces as a ConfigError naming the
    variable. One that does not is one a caller cannot handle uniformly.
    """

    @pytest.mark.parametrize(
        "key,value",
        [
            ("MEET_MAX_AGE_HOURS", "soon"),
            ("MEET_MAX_RUNS_PER_POLL", "lots"),
            ("MEET_MAX_RUNS_PER_POLL", "3.5"),
        ],
    )
    def test_a_non_numeric_limit_is_a_config_error_naming_it(self, key, value):
        with pytest.raises(ConfigError) as caught:
            MeetConfig.from_env({"MEET_ACCESS_TOKEN": "t", key: value})
        assert key in str(caught.value)

    def test_an_empty_value_falls_back_to_the_default(self):
        """An exported-but-blank var is 'unset', not 'zero'."""
        config = MeetConfig.from_env(
            {"MEET_ACCESS_TOKEN": "t", "MEET_MAX_AGE_HOURS": ""}
        )
        assert config.max_age_hours == 24.0


def test_conferences_returns_the_api_order_untouched():
    """The docstring claimed "newest first" with no orderBy and no sort.

    Callers take ``max_runs_per_poll`` off the front, so a false ordering claim
    means arbitrary meetings get picked while looking deliberate. Asserted on
    behaviour rather than on the docstring text: the first version of this test
    grepped for the phrase and failed against the comment *explaining* the fix.
    """
    import json

    from meetings.meet import MeetClient

    order = ["conferenceRecords/c1", "conferenceRecords/c2", "conferenceRecords/c3"]
    body = json.dumps(
        {
            "conferenceRecords": [
                {"name": n, "endTime": "2026-08-31T10:00:00Z"} for n in order
            ]
        }
    ).encode()

    class OneShot:
        def get(self, url, headers):
            return 200, body

    client = MeetClient(MeetConfig(access_token="t"), OneShot())
    assert [c.name for c in client.conferences()] == order
