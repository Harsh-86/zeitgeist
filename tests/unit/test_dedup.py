"""Tests for event deduplication."""

from zeitgeist.sampler.dedup import RecentKeys


def test_dedup_first_seen_returns_false():
    """First encounter of a key should return False."""
    recentkeys = RecentKeys()
    result = recentkeys.seen("key1")
    assert result is False


def test_dedup_repeat_returns_true():
    """Repeat encounter of a key should return True."""
    recentkeys = RecentKeys()
    recentkeys.seen("key1")
    result = recentkeys.seen("key1")
    assert result is True


def test_dedup_expired_by_clock_returns_false():
    """After TTL expiration, a key should return False again."""
    clock_time = 0.0

    def mock_clock():
        return clock_time

    recentkeys = RecentKeys(ttl_seconds=10, clock=mock_clock)

    # See key at time 0
    assert recentkeys.seen("key1") is False

    # Advance clock past TTL
    clock_time = 15.0

    # Key should be expired
    assert recentkeys.seen("key1") is False


def test_dedup_eviction_beyond_max_size():
    """Evict oldest keys when max_size is exceeded."""
    recentkeys = RecentKeys(max_size=3)

    # Add 3 keys
    assert recentkeys.seen("key1") is False
    assert recentkeys.seen("key2") is False
    assert recentkeys.seen("key3") is False

    # Verify they are all seen
    assert recentkeys.seen("key1") is True
    assert recentkeys.seen("key2") is True
    assert recentkeys.seen("key3") is True

    # Add 4th key, should evict oldest (key1)
    assert recentkeys.seen("key4") is False

    # key1 should be gone
    assert recentkeys.seen("key1") is False


def test_dedup_refresh_on_hit():
    """Accessing a key should refresh its TTL."""
    clock_time = 0.0

    def mock_clock():
        return clock_time

    recentkeys = RecentKeys(ttl_seconds=10, clock=mock_clock)

    # See key1 at time 0
    assert recentkeys.seen("key1") is False

    # Advance to time 8 (still within TTL)
    clock_time = 8.0

    # Access key1 again - should refresh (expiry now at time 18)
    assert recentkeys.seen("key1") is True

    # Advance to time 15 (would have expired if not refreshed at time 8)
    clock_time = 15.0

    # key1 should still exist because it was refreshed at time 8
    # and TTL is 10, so expiry should be at time 18
    # Access it again to refresh (expiry now at time 25)
    assert recentkeys.seen("key1") is True

    # Advance to time 30 - past the last refresh at time 15
    # (expiry was at time 15 + 10 = 25)
    clock_time = 30.0

    # Now key1 should be expired (expiry was at time 25)
    assert recentkeys.seen("key1") is False


def test_dedup_multiple_keys_independent():
    """Different keys should be tracked independently."""
    recentkeys = RecentKeys()

    # Add different keys
    assert recentkeys.seen("key_a") is False
    assert recentkeys.seen("key_b") is False

    # Repeat key_a
    assert recentkeys.seen("key_a") is True

    # key_b should still be new in the second call if we don't call it again
    # so we need to test that key_b is still there
    assert recentkeys.seen("key_b") is True
