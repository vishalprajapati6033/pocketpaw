"""Tests for ee.cloud._core.ports — Clock and IdGenerator."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ee.cloud._core.ports import (
    Clock,
    FixedClock,
    FixedIdGenerator,
    IdGenerator,
    ObjectIdGenerator,
    SystemClock,
    get_clock,
    get_id_generator,
    set_clock,
    set_id_generator,
)


class TestSystemClock:
    def test_returns_aware_utc_datetime(self) -> None:
        before = datetime.now(UTC)
        now = SystemClock().now()
        after = datetime.now(UTC)
        assert now.tzinfo is not None
        assert before <= now <= after + timedelta(seconds=1)


class TestFixedClock:
    def test_returns_provided_instant(self) -> None:
        instant = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        clock = FixedClock(instant)
        assert clock.now() == instant
        # Stable across calls
        assert clock.now() == instant


class TestObjectIdGenerator:
    def test_returns_valid_object_id(self) -> None:
        from bson import ObjectId

        gen = ObjectIdGenerator()
        new = gen.new_id()
        assert isinstance(new, str)
        assert ObjectId.is_valid(new)

    def test_returns_unique_ids(self) -> None:
        gen = ObjectIdGenerator()
        ids = {gen.new_id() for _ in range(20)}
        assert len(ids) == 20


class TestFixedIdGenerator:
    def test_returns_provided_ids_in_order(self) -> None:
        gen = FixedIdGenerator(["aaa", "bbb", "ccc"])
        assert gen.new_id() == "aaa"
        assert gen.new_id() == "bbb"
        assert gen.new_id() == "ccc"

    def test_raises_when_exhausted(self) -> None:
        gen = FixedIdGenerator(["only"])
        gen.new_id()
        with pytest.raises(IndexError):
            gen.new_id()


class TestProtocolConformance:
    def test_system_clock_is_clock(self) -> None:
        clock: Clock = SystemClock()
        assert callable(clock.now)

    def test_fixed_clock_is_clock(self) -> None:
        clock: Clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
        assert callable(clock.now)

    def test_object_id_generator_is_id_generator(self) -> None:
        gen: IdGenerator = ObjectIdGenerator()
        assert callable(gen.new_id)


class TestGlobalAccessors:
    def test_default_clock_is_system_clock(self) -> None:
        clock = get_clock()
        assert isinstance(clock, SystemClock)

    def test_default_id_generator_is_object_id_generator(self) -> None:
        gen = get_id_generator()
        assert isinstance(gen, ObjectIdGenerator)

    def test_set_clock_overrides_then_restores(self) -> None:
        original = get_clock()
        try:
            fixed = FixedClock(datetime(2030, 6, 1, tzinfo=UTC))
            set_clock(fixed)
            assert get_clock() is fixed
            assert get_clock().now() == fixed.now()
        finally:
            set_clock(original)
        assert get_clock() is original

    def test_set_id_generator_overrides_then_restores(self) -> None:
        original = get_id_generator()
        try:
            fixed = FixedIdGenerator(["X"])
            set_id_generator(fixed)
            assert get_id_generator() is fixed
        finally:
            set_id_generator(original)
        assert get_id_generator() is original
