"""Tests for client.py — WizClient initialization and properties."""

import asyncio
import base64
import json
import logging
import os
import queue
import threading
import time
from unittest.mock import patch, MagicMock, PropertyMock, call

import pytest

from wizsec.config import Config
from wizsec._registry import EnvironmentRegistry, ProfileRegistry
from wizsec.exceptions import (
    WizAuthenticationError, WizAPIError, WizCredentialsError,
    WizTimeoutError,
)
from wizsec._transport import TransportError


@pytest.fixture()
def mock_client(mock_config):
    """Create a WizClient with mocked auth so no real network calls happen."""
    with patch("wizsec.client.WizClient._preload_credentials"), \
         patch("wizsec.client.WizClient._initialize_headers"):
        from wizsec.client import WizClient
        # Clear the singleton cache so we get a fresh instance
        WizClient._clients.clear()
        client = WizClient(environment="gov", profile="test")
        yield client
        WizClient._clients.clear()


class TestWizClientSingleton:
    def test_same_env_profile_returns_same_instance(self, mock_config):
        with patch("wizsec.client.WizClient._preload_credentials"), \
             patch("wizsec.client.WizClient._initialize_headers"):
            from wizsec.client import WizClient
            WizClient._clients.clear()

            c1 = WizClient(environment="app", profile="default")
            c2 = WizClient(environment="app", profile="default")
            assert c1 is c2

            WizClient._clients.clear()

    def test_different_profile_returns_different_instance(self, mock_config):
        with patch("wizsec.client.WizClient._preload_credentials"), \
             patch("wizsec.client.WizClient._initialize_headers"):
            from wizsec.client import WizClient
            WizClient._clients.clear()

            c1 = WizClient(environment="app", profile="p1")
            c2 = WizClient(environment="app", profile="p2")
            assert c1 is not c2

            WizClient._clients.clear()

    def test_different_environment_returns_different_instance(self, mock_config):
        with patch("wizsec.client.WizClient._preload_credentials"), \
             patch("wizsec.client.WizClient._initialize_headers"):
            from wizsec.client import WizClient
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


# ===================================================================
# Helper to build a minimal JWT for testing
# ===================================================================

def _make_jwt(payload: dict) -> str:
    """Build a fake JWT with header.payload.signature (only payload matters)."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256"}).encode()).rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.fakesig"


# ===================================================================
# _initialize_headers
# ===================================================================

class TestInitializeHeaders:
    def test_headers_set_on_env_state(self, mock_config):
        """Create client without mocking _initialize_headers to test it."""
        with patch("wizsec.client.WizClient._preload_credentials"):
            from wizsec.client import WizClient
            WizClient._clients.clear()
            client = WizClient(environment="gov", profile="hdr_test")
            headers = client._env_state.headers
            assert headers["Content-Type"] == "application/json"
            assert "User-Agent" in headers
            assert "wizsec" in headers["User-Agent"]
            WizClient._clients.clear()


# ===================================================================
# _enqueue_request
# ===================================================================

class TestEnqueueRequest:
    def test_serverless_executes_immediately(self, mock_client):
        request = MagicMock()
        with patch.object(Config, "serverless", return_value=True):
            mock_client._enqueue_request(request)
        request._execute_page.assert_called_once()

    def test_serverless_execution_error_appended(self, mock_client):
        request = MagicMock()
        request.errors = []
        request._execute_page.side_effect = RuntimeError("boom")
        request._done_event = threading.Event()
        with patch.object(Config, "serverless", return_value=True):
            mock_client._enqueue_request(request)
        assert len(request.errors) == 1
        assert "boom" in request.errors[0]["message"]
        assert request._done_event.is_set()

    def test_normal_mode_enqueues_and_starts_worker(self, mock_client):
        request = MagicMock()
        with patch.object(Config, "serverless", return_value=False), \
             patch.object(mock_client, "_start_worker") as sw:
            mock_client._enqueue_request(request)
        assert not mock_client._env_state.queue.empty()
        sw.assert_called_once()


# ===================================================================
# _start_worker / _process_queue / stop_worker
# ===================================================================

class TestWorkerLifecycle:
    def test_start_worker_creates_thread(self, mock_client):
        mock_client._start_worker()
        env = mock_client._env_state
        assert env.worker_thread is not None
        assert env.worker_thread.is_alive()
        mock_client.stop_worker()

    def test_start_worker_noop_if_already_running(self, mock_client):
        mock_client._start_worker()
        first_thread = mock_client._env_state.worker_thread
        mock_client._start_worker()
        assert mock_client._env_state.worker_thread is first_thread
        mock_client.stop_worker()

    def test_process_queue_executes_request(self, mock_client):
        request = MagicMock()
        mock_client._env_state.queue.put(request)
        mock_client._start_worker()
        mock_client._env_state.queue.join()  # wait for processing
        mock_client.stop_worker()
        request._execute_page.assert_called_once()

    def test_stop_worker_clears_thread(self, mock_client):
        mock_client._start_worker()
        mock_client.stop_worker()
        assert mock_client._env_state.worker_thread is None


# ===================================================================
# create_request / create_batch_request
# ===================================================================

class TestRequestCreation:
    def test_create_request_returns_wiz_response(self, mock_client):
        with patch("wizsec._request.WizRequest") as MockReq, \
             patch("wizsec._request.WizResponse") as MockResp:
            result = mock_client.create_request(query="{ viewer { id } }")
        MockReq.assert_called_once()
        MockResp.assert_called_once()

    def test_create_batch_request_returns_batch(self, mock_client):
        with patch("wizsec._request.WizBatchRequest") as MockBatch:
            result = mock_client.create_batch_request()
        MockBatch.assert_called_once_with(client=mock_client)


# ===================================================================
# _post
# ===================================================================

class TestPost:
    def test_post_delegates_to_transport(self, mock_client):
        mock_client._env_state.dc = "us1"
        mock_client._domain = "gov.wiz.io"
        mock_resp = MagicMock(status_code=200)
        with patch("wizsec.client.transport_post", return_value=mock_resp) as tp:
            result = mock_client._post(url="https://api.us1.gov.wiz.io/graphql", json={"query": "{ v }"})
        tp.assert_called_once()
        assert result is mock_resp

    def test_post_raises_transport_error(self, mock_client):
        mock_client._env_state.dc = "us1"
        mock_client._domain = "gov.wiz.io"
        with patch("wizsec.client.transport_post", side_effect=TransportError("fail")):
            with pytest.raises(TransportError):
                mock_client._post(url="https://api.us1.gov.wiz.io/graphql", json={})


# ===================================================================
# _limiter_key / _get_limiter
# ===================================================================

class TestRateLimiting:
    def test_limiter_key_query_service(self, mock_client):
        request = MagicMock()
        request._current_query_info = {"request_type": "query"}
        mock_client._is_service_account = True
        assert mock_client._limiter_key(request) == "query_service"

    def test_limiter_key_mutation_user(self, mock_client):
        request = MagicMock()
        request._current_query_info = {"request_type": "mutation"}
        mock_client._is_service_account = False
        assert mock_client._limiter_key(request) == "mutation_user"

    def test_limiter_key_defaults_to_query(self, mock_client):
        assert mock_client._limiter_key(None) == "query_service"

    def test_get_limiter_delegates_to_env_state(self, mock_client):
        limiter = mock_client._get_limiter("query_service")
        assert limiter is not None


# ===================================================================
# _check_token
# ===================================================================

class TestCheckToken:
    def test_no_token_triggers_authenticate(self, mock_client):
        mock_client._profile_state.token_data = {}
        with patch.object(mock_client, "_authenticate") as auth, \
             patch.object(Config, "serverless", return_value=False):
            mock_client._check_token()
        auth.assert_called_once()

    def test_expired_token_triggers_refresh(self, mock_client):
        mock_client._profile_state.token_data = {"access_token": "tok", "time_received": 0}
        with patch.object(mock_client, "_authenticate") as auth, \
             patch.object(mock_client, "_token_expired", return_value=True), \
             patch.object(Config, "serverless", return_value=False):
            mock_client._check_token()
        auth.assert_called_once_with(refresh=True)

    def test_valid_token_no_auth(self, mock_client):
        mock_client._profile_state.token_data = {"access_token": "tok", "time_received": time.time()}
        with patch.object(mock_client, "_authenticate") as auth, \
             patch.object(mock_client, "_token_expired", return_value=False), \
             patch.object(Config, "serverless", return_value=False):
            mock_client._check_token()
        auth.assert_not_called()

    def test_serverless_forces_fresh_auth(self, mock_client):
        mock_client._profile_state.token_data = {"access_token": "tok", "time_received": time.time()}
        with patch.object(mock_client, "_authenticate") as auth, \
             patch.object(mock_client, "_token_expired", return_value=False), \
             patch.object(Config, "serverless", return_value=True):
            mock_client._check_token()
        # Called once for serverless, then token_data is truthy and not expired so no more
        auth.assert_called_once()


# ===================================================================
# Credential loading
# ===================================================================

class TestCredentialLoading:
    def test_preload_skips_non_client_credentials(self, mock_client):
        mock_client._grant_type = "device_code"
        with patch.object(mock_client, "_load_credentials_by_storage_method") as load:
            mock_client._preload_credentials()
        load.assert_not_called()

    def test_preload_calls_load_and_validate(self, mock_config):
        """Use a fresh client (not mock_client) so _preload_credentials is real."""
        from wizsec.client import WizClient
        with patch("wizsec.client.WizClient._initialize_headers"), \
             patch.object(WizClient, "_load_credentials_by_storage_method") as load, \
             patch.object(WizClient, "_validate_and_store_credentials") as validate:
            WizClient._clients.clear()
            client = WizClient(environment="gov", profile="preload_test")
        load.assert_called_once()
        validate.assert_called_once()
        WizClient._clients.clear()

    def test_load_credentials_from_env(self, mock_client):
        with patch.dict(os.environ, {"WIZ_CLIENT_ID": "env-id", "WIZ_CLIENT_SECRET": "env-secret"}):
            mock_client._load_credentials_from_env()
        assert mock_client._client_id == "env-id"
        assert mock_client._client_secret == "env-secret"

    def test_load_credentials_from_env_profile_prefixed(self, mock_client):
        with patch.dict(os.environ, {"test_WIZ_CLIENT_ID": "pid", "test_WIZ_CLIENT_SECRET": "psec"}, clear=False):
            # Clear the non-prefixed vars to test fallback
            env = {k: v for k, v in os.environ.items()
                   if k not in ("WIZ_CLIENT_ID", "WIZ_CLIENT_SECRET")}
            with patch.dict(os.environ, env, clear=True):
                mock_client._load_credentials_from_env()
        assert mock_client._client_id == "pid"
        assert mock_client._client_secret == "psec"

    def test_load_by_storage_env_falls_through(self, mock_client):
        mock_client.credential_storage = "env"
        mock_client._client_id = ""
        mock_client._client_secret = ""
        with patch.object(mock_client, "_load_credentials_from_env") as load_env:
            mock_client._load_credentials_by_storage_method()
        load_env.assert_called_once()

    def test_load_by_storage_file(self, mock_client):
        mock_client.credential_storage = "file"
        mock_client._client_id = ""
        mock_client._client_secret = ""
        with patch.object(mock_client, "_load_credentials_from_file") as load_file, \
             patch.object(mock_client, "_load_credentials_from_env"):
            mock_client._load_credentials_by_storage_method()
        load_file.assert_called_once()

    def test_load_by_storage_prompt(self, mock_client):
        mock_client.credential_storage = "prompt"
        mock_client._client_id = ""
        mock_client._client_secret = ""
        with patch.object(mock_client, "_load_credentials_from_prompt") as load_prompt, \
             patch.object(mock_client, "_load_credentials_from_env"):
            mock_client._load_credentials_by_storage_method()
        load_prompt.assert_called_once()

    def test_load_by_storage_direct_creds_skip(self, mock_client):
        """When client_id and client_secret are already set, skip loading."""
        mock_client._client_id = "direct-id"
        mock_client._client_secret = "direct-secret"
        mock_client.credential_storage = "env"
        with patch.object(mock_client, "_load_credentials_from_env") as load_env:
            mock_client._load_credentials_by_storage_method()
        load_env.assert_not_called()


# ===================================================================
# _validate_and_store_credentials
# ===================================================================

class TestValidateAndStoreCredentials:
    def test_raises_when_missing_id(self, mock_client):
        mock_client._client_id = ""
        mock_client._client_secret = "secret"
        with pytest.raises(WizCredentialsError):
            mock_client._validate_and_store_credentials()

    def test_raises_when_missing_secret(self, mock_client):
        mock_client._client_id = "id"
        mock_client._client_secret = ""
        with pytest.raises(WizCredentialsError):
            mock_client._validate_and_store_credentials()

    def test_stores_credentials_in_profile_state(self, mock_client):
        mock_client._client_id = "myid"
        mock_client._client_secret = "mysec"
        mock_client.serverless = True  # skip file save
        mock_client._validate_and_store_credentials()
        assert mock_client._profile_state.client_id == "myid"
        assert mock_client._profile_state.client_secret == "mysec"


# ===================================================================
# _authenticate / _authenticate_client_credentials
# ===================================================================

class TestAuthentication:
    def test_authenticate_dispatches_client_credentials(self, mock_client):
        with patch.object(mock_client, "_authenticate_client_credentials") as acc:
            mock_client._authenticate()
        acc.assert_called_once()

    def test_authenticate_dispatches_device_code(self, mock_client):
        mock_client._grant_type = "device_code"
        with patch.object(mock_client, "_authenticate_device_code") as adc:
            mock_client._authenticate()
        adc.assert_called_once()

    def test_client_credentials_success(self, mock_client):
        mock_client._profile_state.client_id = "cid"
        mock_client._profile_state.client_secret = "csec"
        mock_client._domain = "gov.wiz.io"
        mock_client._proxies = {}

        token_payload = {"dc": "us35", "isServiceAccount": True}
        jwt = _make_jwt(token_payload)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": jwt,
            "token_type": "Bearer",
            "expires_in": 3600,
        }

        with patch("wizsec.client.transport_post", return_value=mock_resp):
            result = mock_client._authenticate_client_credentials()

        assert result is True
        assert mock_client._profile_state.token_data["access_token"] == jwt
        assert mock_client.dc == "us35"
        assert "Authorization" in mock_client._env_state.headers

    def test_client_credentials_non_200_raises(self, mock_client):
        mock_client._profile_state.client_id = "cid"
        mock_client._profile_state.client_secret = "csec"
        mock_client._domain = "gov.wiz.io"
        mock_client._proxies = {}

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "Unauthorized"

        with patch("wizsec.client.transport_post", return_value=mock_resp):
            with pytest.raises(WizAuthenticationError):
                mock_client._authenticate_client_credentials()

    def test_client_credentials_transport_error(self, mock_client):
        mock_client._profile_state.client_id = "cid"
        mock_client._profile_state.client_secret = "csec"
        mock_client._domain = "gov.wiz.io"
        mock_client._proxies = {}

        with patch("wizsec.client.transport_post", side_effect=TransportError("net fail")):
            with pytest.raises(WizAPIError):
                mock_client._authenticate_client_credentials()


# ===================================================================
# _decode_access_token
# ===================================================================

class TestDecodeAccessToken:
    def test_decode_valid_jwt(self, mock_client):
        jwt = _make_jwt({"dc": "eu5", "isServiceAccount": False})
        result = mock_client._decode_access_token(jwt)
        assert result is True
        assert mock_client.dc == "eu5"
        assert mock_client.is_service_account is False

    def test_decode_invalid_jwt_single_part(self, mock_client):
        result = mock_client._decode_access_token("not-a-jwt")
        assert result is False

    def test_decode_none_token(self, mock_client):
        result = mock_client._decode_access_token(None)
        assert result is False

    def test_decode_empty_string(self, mock_client):
        result = mock_client._decode_access_token("")
        assert result is False

    def test_decode_bad_base64(self, mock_client):
        result = mock_client._decode_access_token("header.!!!invalid!!!.sig")
        assert result is False


# ===================================================================
# _token_expired
# ===================================================================

class TestTokenExpired:
    def test_no_access_token_means_expired(self, mock_client):
        mock_client._profile_state.token_data = {}
        assert mock_client._token_expired() is True

    def test_recent_token_not_expired(self, mock_client):
        mock_client._profile_state.token_data = {
            "access_token": "tok",
            "time_received": time.time(),
        }
        assert mock_client._token_expired() is False

    def test_old_token_is_expired(self, mock_client):
        mock_client._profile_state.token_data = {
            "access_token": "tok",
            "time_received": time.time() - 7200,  # 2 hours ago
        }
        assert mock_client._token_expired() is True


# ===================================================================
# loud_logging
# ===================================================================

class TestLoudLogging:
    def test_loud_logging_noop_when_verbose_disabled(self, mock_client):
        mock_client._verbose = False
        with mock_client.loud_logging():
            pass  # should not raise

    def test_loud_logging_sets_and_restores_level(self, mock_client):
        mock_client._verbose = True
        handler = MagicMock()
        handler.level = logging.WARNING
        mock_client._logger.console_handler = handler
        mock_client._logger.initial_console_level = logging.WARNING

        with mock_client.loud_logging():
            handler.setLevel.assert_called_with(15)  # VERBOSE level
        # After exit, should restore
        assert handler.setLevel.call_args_list[-1] == call(logging.WARNING)

    def test_loud_logging_already_verbose_no_change(self, mock_client):
        mock_client._verbose = True
        handler = MagicMock()
        handler.level = 15  # already VERBOSE
        mock_client._logger.console_handler = handler

        with mock_client.loud_logging():
            handler.setLevel.assert_not_called()


# ===================================================================
# set_log_level
# ===================================================================

class TestSetLogLevel:
    def test_set_log_level_delegates_to_config(self, mock_client):
        with patch.object(Config, "set_log_level") as sll, \
             patch.object(Config, "get_logger", return_value=mock_client._logger):
            mock_client.set_log_level("DEBUG")
        sll.assert_called_once_with("DEBUG", handler_level=None, include_children=True)


# ===================================================================
# cleanup_for_lambda
# ===================================================================

class TestCleanupForLambda:
    def test_cleanup_when_serverless(self, mock_client):
        mock_client.serverless = True
        with patch.object(ProfileRegistry, "cleanup") as pc, \
             patch.object(EnvironmentRegistry, "cleanup") as ec:
            mock_client.cleanup_for_lambda()
        pc.assert_called_once_with("test")
        ec.assert_called_once_with("gov")

    def test_cleanup_noop_when_not_serverless(self, mock_client):
        mock_client.serverless = False
        with patch.object(ProfileRegistry, "cleanup") as pc, \
             patch.object(EnvironmentRegistry, "cleanup") as ec:
            mock_client.cleanup_for_lambda()
        pc.assert_not_called()
        ec.assert_not_called()


# ===================================================================
# async_session
# ===================================================================

class TestAsyncSession:
    def test_async_session_creates_and_closes_client(self, mock_client):
        from unittest.mock import AsyncMock
        mock_async_client = MagicMock()
        mock_async_client.aclose = AsyncMock()

        async def _run():
            with patch("wizsec.client.create_async_client", return_value=mock_async_client):
                async with mock_client.async_session() as sc:
                    assert sc is mock_client
                    assert hasattr(sc, "_async_session")
            # After exit, _async_session should be removed
            assert not hasattr(mock_client, "_async_session")
            mock_async_client.aclose.assert_awaited_once()

        asyncio.run(_run())

    def test_async_session_with_provided_client(self, mock_client):
        from unittest.mock import AsyncMock
        provided = MagicMock()
        provided.aclose = AsyncMock()

        async def _run():
            async with mock_client.async_session(client=provided) as sc:
                assert sc._async_session is provided
            # Provided client should NOT be closed (not owned)
            provided.aclose.assert_not_awaited()

        asyncio.run(_run())
