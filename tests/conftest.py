"""Shared fixtures for wiz-sdk tests."""

import logging
import os
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure the VERBOSE level is always registered before any test runs
# ---------------------------------------------------------------------------
from wiz_sdk._logging import _register_verbose_level

_register_verbose_level()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_config(tmp_path: Path) -> dict:
    """Return a minimal in-memory config dict."""
    return {
        "app": {
            "name": "wiz-sdk",
            "release": "1.0.0",
        },
        "auth": {
            "grant_type": "client_credentials",
            "credentials": {"storage_method": "env"},
        },
        "domain": {"default": "gov"},
        "api": {
            "max_retries": 2,
            "retry_time": 0.01,
            "timeout": 5,
        },
        "logging": {
            "enabled": False,
        },
        "saved_data": {
            "enabled": True,
            "directory": str(tmp_path / "saved-data"),
        },
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def minimal_config(tmp_path):
    """Provide a minimal config dict."""
    return _minimal_config(tmp_path)


@pytest.fixture(autouse=True)
def _reset_config():
    """Reset Config class state between tests to avoid leaking."""
    from wiz_sdk.config import Config

    yield

    Config._CONFIG = None
    Config._logger = None
    Config._loaded = False


@pytest.fixture(autouse=True)
def _reset_registries():
    """Reset environment and profile registries between tests."""
    from wiz_sdk._registry import EnvironmentRegistry, ProfileRegistry

    yield

    EnvironmentRegistry.cleanup_all()
    ProfileRegistry.cleanup_all()


@pytest.fixture()
def mock_config(minimal_config):
    """Patch Config so it behaves as loaded with a minimal config dict."""
    from wiz_sdk.config import Config

    Config._CONFIG = minimal_config
    Config._loaded = True
    Config._logger = logging.getLogger("wiz_sdk")
    return Config
