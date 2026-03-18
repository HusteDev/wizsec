"""
Per-environment and per-profile shared state registries.

Wiz enforces API rate limits at the environment level. All WizClient instances
targeting the same environment (app, gov, fedramp) MUST share a single queue,
rate limiter set, and worker thread to stay within those limits.

Per-profile state (tokens, credentials) is separate — multiple profiles can
coexist within the same environment.
"""

import queue
import threading
from typing import Dict, Optional
from pyrate_limiter import Duration, Limiter, Rate


class EnvironmentState:
    """Holds per-environment shared resources.

    Everything in this class is shared across all WizClient instances
    that target the same environment. This is intentional — Wiz rate
    limits are global per environment.
    """

    def __init__(self, environment: str):
        self.environment = environment
        self.queue: queue.Queue = queue.Queue()
        self.worker_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.queue_lock = threading.Lock()
        self.headers: Dict[str, str] = {"Content-Type": "application/json"}
        self.dc: Optional[str] = None
        self._limiters: Optional[Dict[str, Limiter]] = None

    @property
    def limiters(self) -> Dict[str, Limiter]:
        if self._limiters is None:
            rate_configs = {
                "query_user": Rate(100, Duration.SECOND),
                "query_service": Rate(10, Duration.SECOND),
                "mutation_user": Rate(10, Duration.SECOND),
                "mutation_service": Rate(3, Duration.SECOND),
            }
            self._limiters = {
                k: Limiter(v, max_delay=1000) for k, v in rate_configs.items()
            }
        return self._limiters

    def get_limiter(self, key: str) -> Limiter:
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

    def __init__(self, profile: str):
        self.profile = profile
        self.token_data: Dict = {}
        self.client_id: Optional[str] = None
        self.client_secret: Optional[str] = None
        self.auth_lock = threading.Lock()

    @property
    def access_token(self) -> Optional[str]:
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
