"""Tests for the exception hierarchy."""

import pytest

from wiz_sdk.exceptions import (
    WizAPIError,
    WizAuthenticationError,
    WizConfigurationError,
    WizCredentialsError,
    WizError,
    WizFileError,
    WizQueryError,
    WizRateLimitError,
    WizReportError,
    WizServerlessError,
    WizTimeoutError,
)


class TestWizError:
    def test_base_error(self):
        err = WizError("something broke")
        assert str(err) == "something broke"
        assert err.original_error is None

    def test_base_error_with_original(self):
        orig = ValueError("root cause")
        err = WizError("wrapper", original_error=orig)
        assert err.original_error is orig

    def test_all_subclasses_inherit_wiz_error(self):
        subclasses = [
            WizAuthenticationError,
            WizAPIError,
            WizConfigurationError,
            WizCredentialsError,
            WizRateLimitError,
            WizQueryError,
            WizReportError,
            WizTimeoutError,
            WizFileError,
            WizServerlessError,
        ]
        for cls in subclasses:
            assert issubclass(cls, WizError)


class TestWizAPIError:
    def test_status_code_and_response_text(self):
        err = WizAPIError("bad request", status_code=400, response_text="invalid")
        assert err.status_code == 400
        assert err.response_text == "invalid"

    def test_defaults_to_none(self):
        err = WizAPIError("fail")
        assert err.status_code is None
        assert err.response_text is None


class TestWizRateLimitError:
    def test_retry_after(self):
        err = WizRateLimitError("slow down", retry_after=30)
        assert err.retry_after == 30


class TestWizQueryError:
    def test_query_and_errors(self):
        err = WizQueryError("bad query", query="{ foo }", errors=[{"message": "nope"}])
        assert err.query == "{ foo }"
        assert len(err.errors) == 1

    def test_errors_default_to_empty_list(self):
        err = WizQueryError("bad")
        assert err.errors == []


class TestWizReportError:
    def test_report_fields(self):
        err = WizReportError("report fail", report_id="r1", report_name="My Report")
        assert err.report_id == "r1"
        assert err.report_name == "My Report"


class TestExceptionCatching:
    """Verify that catching WizError catches all subtypes."""

    def test_catch_api_error_as_wiz_error(self):
        with pytest.raises(WizError):
            raise WizAPIError("test", status_code=500)

    def test_catch_auth_error_as_wiz_error(self):
        with pytest.raises(WizError):
            raise WizAuthenticationError("test")
