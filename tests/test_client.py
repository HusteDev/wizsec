"""Tests for client.py — WizClient initialization and properties."""

import logging
import threading
from unittest.mock import patch, MagicMock, PropertyMock

import pytest

from wiz_sdk.config import Config
from wiz_sdk._registry import EnvironmentRegistry, ProfileRegistry


@pytest.fixture()
def mock_client(mock_config):
    """Create a WizClient with mocked auth so no real network calls happen."""
    with patch("wiz_sdk.client.WizClient._preload_credentials"), \
         patch("wiz_sdk.client.WizClient._initialize_headers"):
        from wiz_sdk.client import WizClient
        # Clear the singleton cache so we get a fresh instance
        WizClient._clients.clear()
        client = WizClient(environment="gov", profile="test")
        yield client
        WizClient._clients.clear()


class TestWizClientSingleton:
    def test_same_env_profile_returns_same_instance(self, mock_config):
        with patch("wiz_sdk.client.WizClient._preload_credentials"), \
             patch("wiz_sdk.client.WizClient._initialize_headers"):
            from wiz_sdk.client import WizClient
            WizClient._clients.clear()

            c1 = WizClient(environment="app", profile="default")
            c2 = WizClient(environment="app", profile="default")
            assert c1 is c2

            WizClient._clients.clear()

    def test_different_profile_returns_different_instance(self, mock_config):
        with patch("wiz_sdk.client.WizClient._preload_credentials"), \
             patch("wiz_sdk.client.WizClient._initialize_headers"):
            from wiz_sdk.client import WizClient
            WizClient._clients.clear()

            c1 = WizClient(environment="app", profile="p1")
            c2 = WizClient(environment="app", profile="p2")
            assert c1 is not c2

            WizClient._clients.clear()

    def test_different_environment_returns_different_instance(self, mock_config):
        with patch("wiz_sdk.client.WizClient._preload_credentials"), \
             patch("wiz_sdk.client.WizClient._initialize_headers"):
            from wiz_sdk.client import WizClient
            WizClient._clients.clear()

            c1 = WizClient(environment="app", profile="default")
            c2 = WizClient(environment="gov", profile="default")
            assert c1 is not c2

            WizClient._clients.clear()


class TestWizClientProperties:
    def test_environment_set(self, mock_client):
        assert mock_client.environment == "gov"

    def test_profile_set(self, mock_client):
        assert mock_client.profile == "test"

    def test_is_service_account_default(self, mock_client):
        # Default grant_type is client_credentials, so service account
        assert mock_client.is_service_account is True

    def test_access_token_property(self, mock_client):
        mock_client.access_token = "test-token-123"
        assert mock_client.access_token == "test-token-123"

    def test_client_id_property(self, mock_client):
        mock_client.client_id = "test-client-id"
        assert mock_client.client_id == "test-client-id"

    def test_client_secret_property(self, mock_client):
        mock_client.client_secret = "test-secret"
        assert mock_client.client_secret == "test-secret"

    def test_dc_property(self, mock_client):
        # dc comes from environment state, initially None
        assert mock_client.dc is None

    def test_dc_setter(self, mock_client):
        mock_client._env_state.dc = "us1"
        assert mock_client.dc == "us1"


class TestWizClientInit:
    def test_retry_config_from_config(self, mock_client):
        assert mock_client._query_retry_time == 0.01  # from minimal_config
        assert mock_client._max_retries == 2

    def test_registry_state_created(self, mock_client):
        assert mock_client._env_state is not None
        assert mock_client._profile_state is not None

    def test_initialized_flag(self, mock_client):
        assert mock_client._initialized is True


class TestApiEndpoint:
    def test_endpoint_format(self, mock_client):
        mock_client._env_state.dc = "us1"
        mock_client._domain = "gov.wiz.io"
        endpoint = mock_client._api_endpoint()
        assert "us1" in endpoint
        assert "gov.wiz.io" in endpoint
        assert endpoint.endswith("/graphql")
