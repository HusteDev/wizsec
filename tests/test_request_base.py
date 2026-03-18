"""Tests for _RequestBase shared logic."""

import pytest


class TestMergePage:
    """Test the _merge_page logic used by both sync and async requests."""

    def _make_base(self):
        """Create a minimal _RequestBase instance for testing."""
        from wiz_sdk._request import _RequestBase

        obj = object.__new__(_RequestBase)
        obj._aggregated_data = None
        return obj

    def test_first_page_sets_data(self):
        base = self._make_base()
        base._merge_page({"users": {"nodes": [1, 2], "pageInfo": {"hasNextPage": True}}})
        assert base._aggregated_data["users"]["nodes"] == [1, 2]

    def test_second_page_extends_nodes(self):
        base = self._make_base()
        base._merge_page({"users": {"nodes": [1, 2], "pageInfo": {"hasNextPage": True}}})
        base._merge_page({"users": {"nodes": [3, 4], "pageInfo": {"hasNextPage": False}}})
        assert base._aggregated_data["users"]["nodes"] == [1, 2, 3, 4]

    def test_non_node_fields_overwritten(self):
        base = self._make_base()
        base._merge_page({"count": 10})
        base._merge_page({"count": 20})
        assert base._aggregated_data["count"] == 20

    def test_none_page_data(self):
        base = self._make_base()
        base._merge_page(None)
        assert base._aggregated_data == {}


class TestPageInfo:
    def _make_base(self):
        from wiz_sdk._request import _RequestBase

        obj = object.__new__(_RequestBase)
        return obj

    def test_extracts_page_info(self):
        base = self._make_base()
        data = {"users": {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": "abc"}}}
        info = base._page_info(data)
        assert info == {"hasNextPage": True, "endCursor": "abc"}

    def test_no_page_info_returns_none(self):
        base = self._make_base()
        assert base._page_info({"simple": "data"}) is None

    def test_none_data_returns_none(self):
        base = self._make_base()
        assert base._page_info(None) is None


class TestCleanPageInfo:
    def _make_base(self):
        from wiz_sdk._request import _RequestBase

        obj = object.__new__(_RequestBase)
        return obj

    def test_removes_page_info(self):
        base = self._make_base()
        base.data = {"users": {"nodes": [1], "pageInfo": {"hasNextPage": False}}}
        base._clean_page_info()
        assert "pageInfo" not in base.data["users"]

    def test_no_data_does_not_raise(self):
        base = self._make_base()
        base.data = None
        base._clean_page_info()  # should not raise


class TestSuccess:
    def _make_base(self):
        from wiz_sdk._request import _RequestBase

        obj = object.__new__(_RequestBase)
        return obj

    def test_success_with_data_no_errors(self):
        base = self._make_base()
        base.data = {"result": True}
        base.errors = []
        assert base.success() is True

    def test_failure_with_errors(self):
        base = self._make_base()
        base.data = {"result": True}
        base.errors = [{"message": "oops"}]
        assert base.success() is False

    def test_failure_with_no_data(self):
        base = self._make_base()
        base.data = None
        base.errors = []
        assert base.success() is False
