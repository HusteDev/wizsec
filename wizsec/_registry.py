##
##
##   RRRRR   EEEEE    GGGGG   IIIIIII   SSSSS   TTTTTTT  RRRRR   Y     Y
##   R   R   E       G           I     S          T      R   R    Y   Y
##   RRRRR   EEEEE   G   GGG     I     SSSSS      T      RRRRR     Y
##   R  R    E       G     G     I         S      T      R  R      Y
##   R   R   EEEEE    GGGGG   IIIIIII  SSSSS      T      R   R     Y
##
##
##

import queue
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from pyrate_limiter import Limiter, Rate

# Wiz's published per-tenant rate limits (requests/second) by request type
# and account type. Local limiters run at a configurable fraction of these
# (rate_limit.headroom, default 0.8) so normal operation stays clear of
# server-side 429s; explicit per-key rate_limit.overrides win over the
# headroom scaling.
PUBLISHED_RATE_LIMITS: Dict[str, float] = {
    "query_user": 100.0,
    "query_service": 10.0,
    "mutation_user": 10.0,
    "mutation_service": 3.0,
}


def _build_rates(rps: float) -> List[Rate]:
    """Build the Rate list for a requests-per-second budget.

    A single spacing rate (1 request per interval) both enforces the
    sustained budget and prevents the token bucket from releasing all
    tokens at once (burst), which is what triggers server-side 429s.
    Fractional budgets are preserved via the interval (e.g. 2.4/s ->
    1 request per 417 ms).
    """
    interval_ms = max(1, round(1000.0 / rps))
    return [Rate(1, interval_ms)]


class EnvironmentState:
    """Holds per-environment shared resources.

    Everything in this class is shared across all WizClient instances
    that target the same environment. This is intentional — Wiz rate
    limits are global per environment.
    """

    def __init__(self, environment: str) -> None:
        self.environment = environment
        self.queue: queue.Queue[Any] = queue.Queue()
        self.worker_thread: Optional[threading.Thread] = None
        self.stop_event: threading.Event = threading.Event()
        self.queue_lock: threading.Lock = threading.Lock()
        self.headers: Dict[str, str] = {"Content-Type": "application/json"}
        self.dc: Optional[str] = None
        self._limiters: Optional[Dict[str, Limiter]] = None
        # Cached split entities (cloud accounts or projects) for query splitting.
        # None = not yet fetched; [] = fetch returned empty; list = cached results.
        self._cached_split_entities: Optional[List[Dict[str, Any]]] = None
        self._split_entities_lock: threading.Lock = threading.Lock()
        self._rate_backoff_lock: threading.Lock = threading.Lock()
        self._rate_backoff_until: float = 0.0

    @property
    def limiters(self) -> Dict[str, Limiter]:
        """Lazily initialize and return the per-request-type rate limiters.

        Effective budgets are the published Wiz limits scaled by the
        configured headroom fraction, unless an explicit per-key override
        is configured.
        """
        if self._limiters is None:
            from .config import Config  # local import to avoid a cycle

            headroom = Config.rate_limit_headroom()
            overrides = Config.rate_limit_overrides()
            self._limiters = {
                key: Limiter(_build_rates(overrides.get(key, published * headroom)))
                for key, published in PUBLISHED_RATE_LIMITS.items()
            }
        return self._limiters

    def get_limiter(self, key: str) -> Limiter:
        """Return the rate limiter for the given key, raising ValueError if not found."""
        limiter = self.limiters.get(key)
        if limiter is None:
            raise ValueError(f"No limiter for key: {key}")
        return limiter

    def set_rate_backoff(self, retry_after: int) -> None:
        """Set a shared environment-level server backoff deadline."""
        with self._rate_backoff_lock:
            self._rate_backoff_until = max(
                self._rate_backoff_until, time.monotonic() + retry_after
            )

    def rate_backoff_remaining(self) -> float:
        """Return remaining shared server backoff seconds for this environment."""
        with self._rate_backoff_lock:
            return max(0.0, self._rate_backoff_until - time.monotonic())


class EnvironmentRegistry:
    """Manages per-environment shared state.

    Thread-safe registry. Call get_or_create() to obtain the
    EnvironmentState for a given environment name.
    """

    _environments: Dict[str, EnvironmentState] = {}
    _lock = threading.Lock()

    @classmethod
    def get_or_create(cls, environment: str) -> EnvironmentState:
        """Return the shared state for the environment, creating it if needed."""
        with cls._lock:
            if environment not in cls._environments:
                cls._environments[environment] = EnvironmentState(environment)
            return cls._environments[environment]

    @classmethod
    def cleanup(cls, environment: str) -> None:
        """Clean up a specific environment's state."""
        with cls._lock:
            state = cls._environments.pop(environment, None)
            if state:
                state.stop_event.set()
                if state.worker_thread and state.worker_thread.is_alive():
                    state.worker_thread.join(timeout=1)

    @classmethod
    def cleanup_all(cls) -> None:
        """Clean up all environments."""
        with cls._lock:
            for state in cls._environments.values():
                state.stop_event.set()
                if state.worker_thread and state.worker_thread.is_alive():
                    state.worker_thread.join(timeout=1)
            cls._environments.clear()


class ProfileState:
    """Holds per-profile auth state (tokens, credentials)."""

    def __init__(self, profile: str) -> None:
        self.profile = profile
        self.token_data: Dict[str, Any] = {}
        self.client_id: Optional[str] = None
        self.client_secret: Optional[str] = None
        self.auth_lock: threading.Lock = threading.Lock()

    @property
    def access_token(self) -> Optional[str]:
        """Return the current access token, or None if not authenticated."""
        return self.token_data.get("access_token")

    @access_token.setter
    def access_token(self, value: str) -> None:
        self.token_data["access_token"] = value

    def clear(self) -> None:
        """Clear all auth state for this profile."""
        self.token_data.clear()
        self.client_id = None
        self.client_secret = None


class ProfileRegistry:
    """Manages per-profile auth state.

    Thread-safe registry. Call get_or_create() to obtain the
    ProfileState for a given profile name.
    """

    _profiles: Dict[Tuple[str, str], ProfileState] = {}
    _lock = threading.Lock()

    @classmethod
    def get_or_create(cls, environment: str, profile: str) -> ProfileState:
        """Return the auth state for the profile, creating it if needed."""
        key = (environment, profile)
        with cls._lock:
            if key not in cls._profiles:
                cls._profiles[key] = ProfileState(profile)
            return cls._profiles[key]

    @classmethod
    def cleanup(cls, environment: str, profile: str) -> None:
        """Clean up a specific profile's state."""
        key = (environment, profile)
        with cls._lock:
            state = cls._profiles.pop(key, None)
            if state:
                state.clear()

    @classmethod
    def cleanup_all(cls) -> None:
        """Clean up all profiles."""
        with cls._lock:
            for state in cls._profiles.values():
                state.clear()
            cls._profiles.clear()
