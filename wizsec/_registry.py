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
from typing import Any, Dict, List, Optional
from pyrate_limiter import Duration, Limiter, Rate


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

    @property
    def limiters(self) -> Dict[str, Limiter]:
        """Lazily initialize and return the per-request-type rate limiters."""
        if self._limiters is None:
            # Limiter accepts List[Rate] as its first argument; multiple
            # Rate objects in a list are AND-ed together so both constraints
            # must have capacity before a slot is granted.  Using a fast
            # per-interval rate alongside the sustained cap prevents the
            # token bucket from releasing all tokens at once (burst), which
            # is what triggers server-side 429s.
            rate_configs: Dict[str, List[Rate]] = {
                # query_user: 100/s sustained, no more than 1 per 10 ms
                "query_user":      [Rate(1, 10),  Rate(100, Duration.SECOND)],
                # query_service: 10/s sustained, no more than 1 per 100 ms
                "query_service":   [Rate(1, 100), Rate(10,  Duration.SECOND)],
                # mutation_user: 10/s sustained, no more than 1 per 100 ms
                "mutation_user":   [Rate(1, 100), Rate(10,  Duration.SECOND)],
                # mutation_service: 3/s sustained, no more than 1 per 333 ms
                "mutation_service":[Rate(1, 333), Rate(3,   Duration.SECOND)],
            }
            self._limiters = {k: Limiter(rates) for k, rates in rate_configs.items()}
        return self._limiters

    def get_limiter(self, key: str) -> Limiter:
        """Return the rate limiter for the given key, raising ValueError if not found."""
        limiter = self.limiters.get(key)
        if limiter is None:
            raise ValueError(f"No limiter for key: {key}")
        return limiter


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

    _profiles: Dict[str, ProfileState] = {}
    _lock = threading.Lock()

    @classmethod
    def get_or_create(cls, profile: str) -> ProfileState:
        """Return the auth state for the profile, creating it if needed."""
        with cls._lock:
            if profile not in cls._profiles:
                cls._profiles[profile] = ProfileState(profile)
            return cls._profiles[profile]

    @classmethod
    def cleanup(cls, profile: str) -> None:
        """Clean up a specific profile's state."""
        with cls._lock:
            state = cls._profiles.pop(profile, None)
            if state:
                state.clear()

    @classmethod
    def cleanup_all(cls) -> None:
        """Clean up all profiles."""
        with cls._lock:
            for state in cls._profiles.values():
                state.clear()
            cls._profiles.clear()
