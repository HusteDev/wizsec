"""Tests for wizsec/__init__.py — lazy loading and __all__ exports."""

import pytest


class TestLazyLoading:
    """Verify that __getattr__ lazy-loads the expected names."""

    def test_import_config(self):
        from wizsec import Config

        assert Config is not None
        assert hasattr(Config, "load")

    def test_import_wiz_error(self):
        from wizsec import WizError

        assert issubclass(WizError, Exception)

    def test_import_wiz_authentication_error(self):
        from wizsec import WizAuthenticationError

        assert issubclass(WizAuthenticationError, Exception)

    def test_import_wiz_api_error(self):
        from wizsec import WizAPIError

        assert issubclass(WizAPIError, Exception)

    def test_import_wiz_configuration_error(self):
        from wizsec import WizConfigurationError

        assert issubclass(WizConfigurationError, Exception)

    def test_import_wiz_credentials_error(self):
        from wizsec import WizCredentialsError

        assert issubclass(WizCredentialsError, Exception)

    def test_import_wiz_rate_limit_error(self):
        from wizsec import WizRateLimitError

        assert issubclass(WizRateLimitError, Exception)

    def test_import_wiz_query_error(self):
        from wizsec import WizQueryError

        assert issubclass(WizQueryError, Exception)

    def test_import_wiz_schema_validation_error(self):
        from wizsec import WizSchemaValidationError

        assert issubclass(WizSchemaValidationError, Exception)

    def test_import_wiz_report_error(self):
        from wizsec import WizReportError

        assert issubclass(WizReportError, Exception)

    def test_import_wiz_timeout_error(self):
        from wizsec import WizTimeoutError

        assert issubclass(WizTimeoutError, Exception)

    def test_import_wiz_file_error(self):
        from wizsec import WizFileError

        assert issubclass(WizFileError, Exception)

    def test_import_wiz_serverless_error(self):
        from wizsec import WizServerlessError

        assert issubclass(WizServerlessError, Exception)

    def test_import_environment_registry(self):
        from wizsec import EnvironmentRegistry

        assert hasattr(EnvironmentRegistry, "cleanup_all")

    def test_import_profile_registry(self):
        from wizsec import ProfileRegistry

        assert hasattr(ProfileRegistry, "cleanup_all")

    def test_import_dump_to_json(self):
        from wizsec import dump_to_json

        assert callable(dump_to_json)

    def test_import_load_file_if_in_last_x_interval(self):
        from wizsec import load_file_if_in_last_x_interval

        assert callable(load_file_if_in_last_x_interval)

    def test_import_wiz_batch_request(self):
        from wizsec import WizBatchRequest

        assert WizBatchRequest is not None

    def test_import_wiz_batch_response(self):
        from wizsec import WizBatchResponse

        assert WizBatchResponse is not None

    def test_import_async_wiz_batch_request(self):
        from wizsec import AsyncWizBatchRequest

        assert AsyncWizBatchRequest is not None

    def test_import_nonexistent_raises_attribute_error(self):
        with pytest.raises(AttributeError, match="has no attribute"):
            from wizsec import __init__ as mod

            mod.__getattr__("DoesNotExist")


class TestAllExports:
    """Verify __all__ contains expected public names."""

    def test_all_contains_core_names(self):
        import wizsec

        assert "WizClient" in wizsec.__all__
        assert "Config" in wizsec.__all__

    def test_all_contains_exception_names(self):
        import wizsec

        expected = [
            "WizError",
            "WizAuthenticationError",
            "WizAPIError",
            "WizConfigurationError",
            "WizCredentialsError",
            "WizRateLimitError",
            "WizQueryError",
            "WizSchemaValidationError",
            "WizReportError",
            "WizTimeoutError",
            "WizFileError",
            "WizServerlessError",
        ]
        for name in expected:
            assert name in wizsec.__all__, f"{name} missing from __all__"

    def test_all_contains_utility_names(self):
        import wizsec

        assert "dump_to_json" in wizsec.__all__
        assert "load_file_if_in_last_x_interval" in wizsec.__all__

    def test_all_contains_registry_names(self):
        import wizsec

        assert "EnvironmentRegistry" in wizsec.__all__
        assert "ProfileRegistry" in wizsec.__all__

    def test_all_contains_request_names(self):
        import wizsec

        assert "WizBatchRequest" in wizsec.__all__
        assert "WizBatchResponse" in wizsec.__all__
        assert "AsyncWizBatchRequest" in wizsec.__all__

    def test_all_contains_schema_validator(self):
        import wizsec

        assert "SchemaValidator" in wizsec.__all__
