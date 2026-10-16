"""Unit tests for bot.media_usage — daily per-user media counters."""

from bot import media_usage


class TestMediaUsage:
    def test_get_usage_defaults_zero(self):
        assert media_usage.get_usage(1, media_usage.IMAGE_TYPE, "2026-08-12") == 0

    def test_try_increment_reserves_and_counts(self):
        assert media_usage.try_increment(1, media_usage.IMAGE_TYPE, "2026-08-12", 5) is True
        assert media_usage.get_usage(1, media_usage.IMAGE_TYPE, "2026-08-12") == 1

    def test_try_increment_false_at_limit(self):
        for _ in range(5):
            assert media_usage.try_increment(1, media_usage.IMAGE_TYPE, "2026-08-12", 5) is True
        assert media_usage.try_increment(1, media_usage.IMAGE_TYPE, "2026-08-12", 5) is False
        assert media_usage.get_usage(1, media_usage.IMAGE_TYPE, "2026-08-12") == 5

    def test_try_increment_limit_zero_disables(self):
        for _ in range(3):
            assert media_usage.try_increment(1, media_usage.IMAGE_TYPE, "2026-08-12", 0) is True
        assert media_usage.get_usage(1, media_usage.IMAGE_TYPE, "2026-08-12") == 0

    def test_try_increment_respects_partial_quota(self):
        media_usage.try_increment(1, media_usage.IMAGE_TYPE, "2026-08-12", 2)
        assert media_usage.try_increment(1, media_usage.IMAGE_TYPE, "2026-08-12", 2) is True
        assert media_usage.try_increment(1, media_usage.IMAGE_TYPE, "2026-08-12", 2) is False

    def test_refund_gives_back_slot(self):
        media_usage.try_increment(1, media_usage.IMAGE_TYPE, "2026-08-12", 5)
        media_usage.refund_usage(1, media_usage.IMAGE_TYPE, "2026-08-12")
        assert media_usage.get_usage(1, media_usage.IMAGE_TYPE, "2026-08-12") == 0

    def test_refund_floors_at_zero(self):
        media_usage.refund_usage(1, media_usage.IMAGE_TYPE, "2026-08-12")
        assert media_usage.get_usage(1, media_usage.IMAGE_TYPE, "2026-08-12") == 0

    def test_counters_isolated_per_day(self):
        media_usage.try_increment(1, media_usage.IMAGE_TYPE, "2026-08-12", 5)
        assert media_usage.get_usage(1, media_usage.IMAGE_TYPE, "2026-08-13") == 0

    def test_counters_isolated_per_type(self):
        media_usage.try_increment(1, media_usage.IMAGE_TYPE, "2026-08-12", 5)
        assert media_usage.get_usage(1, media_usage.AUDIO_TYPE, "2026-08-12") == 0

    def test_counters_isolated_per_user(self):
        media_usage.try_increment(1, media_usage.IMAGE_TYPE, "2026-08-12", 5)
        assert media_usage.get_usage(2, media_usage.IMAGE_TYPE, "2026-08-12") == 0

    def test_remaining_positive(self):
        media_usage.try_increment(1, media_usage.IMAGE_TYPE, "2026-08-12", 5)
        assert media_usage.remaining(1, media_usage.IMAGE_TYPE, "2026-08-12", 5) == 4

    def test_remaining_zero_limit_means_unlimited(self):
        media_usage.try_increment(1, media_usage.IMAGE_TYPE, "2026-08-12", 5)
        assert media_usage.remaining(1, media_usage.IMAGE_TYPE, "2026-08-12", 0) == -1
