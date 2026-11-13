"""Asset conditions from facts — rule B (ADR #130 §4)."""

from datetime import date, datetime, timedelta, timezone

import pytest

from polyris.assets import Asset
from polyris.coordination import asset_conditions as ac
from polyris.coordination.spec import parse
from polyris.generators import _serialize_wait_for

UTC = timezone.utc
NOW = datetime(2026, 11, 12, 12, tzinfo=UTC)
D = date(2026, 11, 12)  # a Thursday


def fact(name, pd, *, granularity="daily", at=None):
    return ac.Fact(name, pd, at or datetime.combine(pd, datetime.min.time(), UTC) + timedelta(hours=6), granularity)


class Reader:
    """In-memory FactReader with the ordering contract of the real DAL."""

    def __init__(self, *facts):
        self.facts_by_asset = {}
        for f in facts:
            self.facts_by_asset.setdefault(f.asset_name, []).append(f)
        self.calls = []

    def facts(self, asset_name, *, last, first):
        self.calls.append((asset_name, last, first))
        rows = [f for f in self.facts_by_asset.get(asset_name, [])
                if f.partition_date <= last and (first is None or f.partition_date >= first)]
        return sorted(rows, key=lambda f: f.partition_date, reverse=True)


def spec(*items):
    return parse(_serialize_wait_for(list(items)))


a, b, c = Asset("ns/a"), Asset("ns/b"), Asset("ns/c")


def run(s, reader, *, target=D, now=NOW, known=()):
    return ac.evaluate(s, target=target, now=now, reader=reader, known_facts=known)


class TestLatest:
    def test_daily_fact_for_the_target_date(self):
        result = run(spec(a), Reader(fact("ns/a", D)))
        assert result.ready
        assert result.assets[0].fact.partition_date == D

    def test_daily_fact_for_the_day_before_is_not_enough(self):
        result = run(spec(a), Reader(fact("ns/a", D - timedelta(days=1))))
        assert not result.ready
        assert result.assets[0].reason == ac.STALE

    def test_no_fact_at_all(self):
        assert run(spec(a), Reader()).assets[0].reason == ac.NO_FACT

    def test_backfill_of_a_past_date_waits_for_its_own_partition(self):
        # Today's event must not satisfy a run for 30 days ago (C1).
        result = run(spec(a), Reader(fact("ns/a", D)), target=D - timedelta(days=30))
        assert not result.ready

    def test_hourly_facts_count_for_their_day(self):
        assert run(spec(a), Reader(fact("ns/a", D, granularity="hourly"))).ready

    def test_weekly_fact_from_this_or_last_week(self):
        monday = D - timedelta(days=D.weekday())
        assert run(spec(a), Reader(fact("ns/a", monday, granularity="weekly"))).ready
        assert run(spec(a), Reader(fact("ns/a", monday - timedelta(days=7), granularity="weekly"))).ready
        assert not run(spec(a), Reader(fact("ns/a", monday - timedelta(days=14), granularity="weekly"))).ready

    def test_monthly_fact_from_this_or_last_month(self):
        assert run(spec(a), Reader(fact("ns/a", date(2026, 10, 31), granularity="monthly"))).ready
        assert not run(spec(a), Reader(fact("ns/a", date(2026, 9, 30), granularity="monthly"))).ready

    def test_freshest_satisfying_fact_wins(self):
        older = fact("ns/a", date(2026, 10, 5), granularity="monthly")
        newer = fact("ns/a", date(2026, 11, 2), granularity="monthly")
        assert run(spec(a), Reader(older, newer)).assets[0].fact == newer

    def test_reader_is_asked_only_back_to_the_previous_month(self):
        reader = Reader()
        run(spec(a), reader)
        assert reader.calls == [("ns/a", D, date(2026, 10, 1))]

    def test_unknown_granularity_raises(self):
        with pytest.raises(ValueError):
            run(spec(a), Reader(fact("ns/a", D, granularity="yearly")))


class TestWithin:
    def test_newest_fact_of_the_latest_partition_must_be_fresh(self):
        early = fact("ns/a", D, at=NOW - timedelta(hours=10))
        late = fact("ns/a", D, at=NOW - timedelta(hours=2))
        result = run(spec(a.within(hours=6)), Reader(early, late))
        assert result.ready
        assert result.assets[0].fact == late

    def test_stale_latest_partition(self):
        result = run(spec(a.within(hours=6)), Reader(fact("ns/a", D, at=NOW - timedelta(hours=7))))
        assert not result.ready
        assert result.assets[0].reason == ac.STALE

    def test_backfill_of_an_older_partition_does_not_refresh(self):
        latest = fact("ns/a", D - timedelta(days=1), at=NOW - timedelta(hours=30))
        backfilled = fact("ns/a", D - timedelta(days=5), at=NOW - timedelta(minutes=5))
        assert not run(spec(a.within(hours=6)), Reader(latest, backfilled)).ready

    def test_no_fact(self):
        assert run(spec(a.within(hours=6)), Reader()).assets[0].reason == ac.NO_FACT


class TestConsecutive:
    def test_all_dates_present(self):
        facts = [fact("ns/a", D - timedelta(days=i)) for i in range(3)]
        assert run(spec(a.consecutive(days=3)), Reader(*facts)).ready

    def test_missing_dates_are_listed(self):
        facts = [fact("ns/a", D), fact("ns/a", D - timedelta(days=2))]
        result = run(spec(a.consecutive(days=3)), Reader(*facts))
        assert not result.ready
        assert result.assets[0].missing_dates == (D - timedelta(days=1),)


class TestGroups:
    def test_and_needs_every_member(self):
        assert not run(spec(a, b), Reader(fact("ns/a", D))).ready
        assert run(spec(a, b), Reader(fact("ns/a", D), fact("ns/b", D))).ready

    def test_or_needs_one_member_and_still_reports_all(self):
        result = run(spec(a | b), Reader(fact("ns/b", D)))
        assert result.ready
        assert [(r.asset_name, r.ready) for r in result.assets] == [("ns/a", False), ("ns/b", True)]

    def test_mixed_and_or_is_exact(self):
        s = spec(c, a | b)
        assert not run(s, Reader(fact("ns/a", D))).ready          # c missing
        assert run(s, Reader(fact("ns/a", D), fact("ns/c", D))).ready


class TestEnabledAt:
    """When the spec first held: the newest fact it needs; in an OR group, the earliest ready member."""

    def test_and_dates_from_its_newest_fact(self):
        early, late = NOW - timedelta(hours=3), NOW - timedelta(hours=1)
        result = run(spec(a, b), Reader(fact("ns/a", D, at=late), fact("ns/b", D, at=early)))
        assert result.enabled_at == late

    def test_or_dates_from_its_earliest_ready_member(self):
        early, late = NOW - timedelta(hours=3), NOW - timedelta(hours=1)
        result = run(spec(a | b), Reader(fact("ns/a", D, at=late), fact("ns/b", D, at=early)))
        assert result.enabled_at == early

    def test_a_mixed_spec_takes_the_newest_of_and_and_the_earliest_of_or(self):
        t1, t2, t3 = (NOW - timedelta(hours=h) for h in (5, 3, 1))
        result = run(spec(c, a | b), Reader(fact("ns/c", D, at=t2), fact("ns/a", D, at=t1), fact("ns/b", D, at=t3)))
        assert result.enabled_at == t2                     # c (t2) is newer than the OR's earliest member (t1)

    def test_an_unready_member_does_not_date_an_or(self):
        result = run(spec(a | b), Reader(fact("ns/a", D, at=NOW - timedelta(hours=2)),
                                         fact("ns/b", D - timedelta(days=1), at=NOW - timedelta(hours=9))))
        assert result.enabled_at == NOW - timedelta(hours=2)

    def test_a_spec_that_does_not_hold_has_no_time(self):
        assert run(spec(a, b), Reader(fact("ns/a", D))).enabled_at is None


class TestKnownFacts:
    def test_known_fact_counts_when_the_reader_lags(self):
        assert run(spec(a), Reader(), known=[fact("ns/a", D)]).ready

    def test_known_fact_is_not_duplicated(self):
        f = fact("ns/a", D)
        result = run(spec(a.consecutive(days=1)), Reader(f), known=[f])
        assert result.ready

    def test_known_facts_outside_the_window_are_ignored(self):
        future = fact("ns/a", D + timedelta(days=1))
        assert not run(spec(a), Reader(), known=[future]).ready

    def test_known_and_read_facts_merge_newest_first(self):
        read = [fact("ns/a", D - timedelta(days=d)) for d in (0, 0, 2)]
        read[1] = fact("ns/a", D, at=NOW - timedelta(hours=1))
        known = [fact("ns/a", D - timedelta(days=1)), fact("ns/a", D - timedelta(days=4))]
        result = run(spec(a.consecutive(days=5)), Reader(*read), known=known)
        assert result.assets[0].missing_dates == (D - timedelta(days=3),)


def _weekly_producer(weekday, start, end):
    d = start + timedelta(days=(weekday - start.weekday()) % 7)
    while d <= end:
        yield d
        d += timedelta(days=7)


def _monthly_producer(pick, start, end):
    month = date(start.year, start.month, 1)
    while month <= end:
        yield pick(month)
        month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)


def _last_day(month):
    return (month.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def _first_weekday(month, weekday):
    return month + timedelta(days=(weekday - month.weekday()) % 7)


def _first_business_day(month):
    d = month
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


class TestRuleBLeavesNoUnsatisfiableDates:
    """Every date after a regular producer's first run is served by some fact."""

    START, END = date(2026, 1, 1), date(2027, 12, 31)

    def _assert_every_date_served(self, dates, granularity):
        facts = [ac.Fact("ns/a", d, datetime.combine(d, datetime.min.time(), UTC), granularity) for d in dates]
        first = min(f.partition_date for f in facts)
        day = first
        while day <= self.END:
            assert any(ac.satisfies(f, day) for f in facts), f"{granularity} producer leaves {day} unsatisfiable"
            day += timedelta(days=1)

    @pytest.mark.parametrize("weekday", range(7))
    def test_weekly_on_any_weekday(self, weekday):
        self._assert_every_date_served(list(_weekly_producer(weekday, self.START, self.END)), "weekly")

    @pytest.mark.parametrize("pick", [
        lambda m: m,                          # 1st of the month
        _last_day,                            # last day of the month
        lambda m: _first_weekday(m, 0),       # first Monday
        _first_business_day,                  # first business day
    ], ids=["first-day", "last-day", "first-monday", "first-business-day"])
    def test_monthly_on_regular_schedules(self, pick):
        self._assert_every_date_served(list(_monthly_producer(pick, self.START, self.END)), "monthly")

    def test_a_fact_two_periods_old_is_not_accepted(self):
        assert not ac.satisfies(ac.Fact("ns/a", date(2026, 9, 30), NOW, "monthly"), date(2026, 11, 1))
        assert not ac.satisfies(ac.Fact("ns/a", date(2026, 10, 25), NOW, "weekly"), date(2026, 11, 9))
