"""Event deduplication with LRU and TTL tracking."""

import time
from collections import OrderedDict
from collections.abc import Callable


class RecentKeys:
    """LRU-with-TTL set for tracking recently seen keys.

    Tracks keys with a time-to-live (TTL) and evicts oldest keys when
    the maximum size is exceeded. Accessing a key refreshes its TTL.

    Args:
        max_size: Maximum number of keys to track (default 10000).
        ttl_seconds: Time-to-live in seconds for each key (default 3600).
        clock: Callable that returns current time (default time.monotonic).
    """

    def __init__(
        self,
        max_size: int = 10000,
        ttl_seconds: int = 3600,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        # OrderedDict to maintain insertion order for LRU eviction
        self.keys: OrderedDict[str, float] = OrderedDict()

    def seen(self, key: str) -> bool:
        """Check if a key has been seen recently.

        Returns True if the key was added within the TTL and refreshes
        its expiration time. Returns False if the key is new or expired,
        and records the key.

        Args:
            key: The key to check.

        Returns:
            True if the key was seen within TTL, False if it's new or expired.
        """
        current_time = self.clock()

        # If key exists and hasn't expired, refresh and return True
        if key in self.keys:
            expiration_time = self.keys[key]
            if current_time < expiration_time:
                # Refresh the key by moving to end (most recently used)
                self.keys.move_to_end(key)
                # Update expiration time
                self.keys[key] = current_time + self.ttl_seconds
                return True
            else:
                # Key has expired, remove it
                del self.keys[key]

        # Key is new or expired - record it
        self.keys[key] = current_time + self.ttl_seconds

        # Evict oldest key if we exceed max_size
        if len(self.keys) > self.max_size:
            # Remove the oldest key (first in OrderedDict)
            self.keys.popitem(last=False)

        return False
