"""Tests for the per-environment and per-profile registries."""

import threading

import pytest

from wizsec._registry import (
    EnvironmentRegistry,
    EnvironmentState,
    ProfileRegistry,
    ProfileState,
)


class TestEnvironmentState:
    def test_initial_state(self):
        state = EnvironmentState("app")
        assert state.environment == "app"
        assert state.queue is not None
        assert state.worker_thread is None
        assert not state.stop_event.is_set()
        assert state.dc is None

    def test_headers_default(self):
        state = EnvironmentState("gov")
        assert state.headers == {"Content-Type": "application/json"}

    def test_limiters_lazy_init(self):
        state = EnvironmentState("app")
        assert state._limiters is None
        limiters = state.limiters
        assert "query_user" in limiters
        assert "mutation_service" in limiters
        # Same object on second access
        assert state.limiters is limiters

    def test_get_limiter_valid(self):
        state = EnvironmentState("app")
        limiter = state.get_limiter("query_user")
        assert limiter is not None

    def test_get_limiter_invalid(self):
        state = EnvironmentState("app")
        with pytest.raises(ValueError, match="No limiter for key"):
            state.get_limiter("nonexistent")

    def test_rate_backoff_tracks_remaining_deadline(self):
        state = EnvironmentState("gov")
        assert state.rate_backoff_remaining() == 0
        state.set_rate_backoff(1)
        assert state.rate_backoff_remaining() > 0


class TestEnvironmentRegistry:
    def test_get_or_create_returns_same_instance(self):
        s1 = EnvironmentRegistry.get_or_create("app")
        s2 = EnvironmentRegistry.get_or_create("app")
        assert s1 is s2

    def test_different_environments_get_different_state(self):
        app = EnvironmentRegistry.get_or_create("app")
        gov = EnvironmentRegistry.get_or_create("gov")
        assert app is not gov
        assert app.environment == "app"
        assert gov.environment == "gov"

    def test_cleanup_removes_environment(self):
        EnvironmentRegistry.get_or_create("test_env")
        EnvironmentRegistry.cleanup("test_env")
        # Should create a new instance now
        new_state = EnvironmentRegistry.get_or_create("test_env")
        assert new_state is not None

    def test_cleanup_all(self):
        EnvironmentRegistry.get_or_create("a")
        EnvironmentRegistry.get_or_create("b")
        EnvironmentRegistry.cleanup_all()
        # Both should be fresh
        a = EnvironmentRegistry.get_or_create("a")
        assert a.environment == "a"

    def test_thread_safety(self):
        """Multiple threads creating the same environment should get the same state."""
        results = []

        def grab():
            results.append(EnvironmentRegistry.get_or_create("shared"))

        threads = [threading.Thread(target=grab) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(r is results[0] for r in results)


class TestProfileState:
    def test_initial_state(self):
        state = ProfileState("default")
        assert state.profile == "default"
        assert state.access_token is None
        assert state.client_id is None
        assert state.client_secret is None

    def test_access_token_property(self):
        state = ProfileState("default")
        state.access_token = "tok123"
        assert state.access_token == "tok123"
        assert state.token_data["access_token"] == "tok123"

    def test_clear(self):
        state = ProfileState("default")
        state.access_token = "tok"
        state.client_id = "id"
        state.client_secret = "secret"
        state.clear()
        assert state.access_token is None
        assert state.client_id is None
        assert state.client_secret is None


class TestProfileRegistry:
    def test_get_or_create_returns_same_instance(self):
        p1 = ProfileRegistry.get_or_create("gov", "default")
        p2 = ProfileRegistry.get_or_create("gov", "default")
        assert p1 is p2

    def test_different_profiles(self):
        a = ProfileRegistry.get_or_create("gov", "alpha")
        b = ProfileRegistry.get_or_create("gov", "beta")
        assert a is not b

    def test_same_profile_different_environment(self):
        a = ProfileRegistry.get_or_create("gov", "default")
        b = ProfileRegistry.get_or_create("app", "default")
        assert a is not b

    def test_cleanup(self):
        ProfileRegistry.get_or_create("gov", "tmp")
        ProfileRegistry.cleanup("gov", "tmp")
        new_p = ProfileRegistry.get_or_create("gov", "tmp")
        assert new_p.access_token is None


class TestRateLimitBudgets:
    def test_build_rates_spacing_interval(self):
        from wizsec._registry import _build_rates

        rates = _build_rates(8.0)
        assert len(rates) == 1
        assert rates[0].limit == 1
        assert rates[0].interval == 125

    def test_build_rates_preserves_fractional_budgets(self):
        from wizsec._registry import _build_rates

        assert _build_rates(2.4)[0].interval == 417

    def test_build_rates_interval_floor(self):
        from wizsec._registry import _build_rates

        assert _build_rates(100000.0)[0].interval == 1

    def test_limiters_apply_headroom_and_overrides(self):
        from unittest.mock import patch

        from wizsec._registry import PUBLISHED_RATE_LIMITS
        from wizsec.config import Config

        captured = []

        def fake_build(rps):
            from pyrate_limiter import Rate

            captured.append(rps)
            return [Rate(1, 10)]

        with (
            patch.object(Config, "rate_limit_headroom", return_value=0.5),
            patch.object(
                Config, "rate_limit_overrides", return_value={"query_service": 4.0}
            ),
            patch("wizsec._registry._build_rates", side_effect=fake_build),
        ):
            state = EnvironmentState("headroom-test")
            limiters = state.limiters

        expected = [
            4.0 if key == "query_service" else published * 0.5
            for key, published in PUBLISHED_RATE_LIMITS.items()
        ]
        assert captured == expected
        assert set(limiters) == set(PUBLISHED_RATE_LIMITS)
