"""Tests for _RequestBase shared logic."""

import pytest


class TestMergePage:
    """Test the _merge_page logic used by both sync and async requests."""

    def _make_base(self):
        """Create a minimal _RequestBase instance for testing."""
        from wizsec._request import _RequestBase

        obj = object.__new__(_RequestBase)
        obj._aggregated_data = None
        return obj

    def test_first_page_sets_data(self):
        base = self._make_base()
        base._merge_page(
            {"users": {"nodes": [1, 2], "pageInfo": {"hasNextPage": True}}}
        )
        assert base._aggregated_data["users"]["nodes"] == [1, 2]

    def test_second_page_extends_nodes(self):
        base = self._make_base()
        base._merge_page(
            {"users": {"nodes": [1, 2], "pageInfo": {"hasNextPage": True}}}
        )
        base._merge_page(
            {"users": {"nodes": [3, 4], "pageInfo": {"hasNextPage": False}}}
        )
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
        from wizsec._request import _RequestBase

        obj = object.__new__(_RequestBase)
        return obj

    def test_extracts_page_info(self):
        base = self._make_base()
        data = {
            "users": {
                "nodes": [],
                "pageInfo": {"hasNextPage": True, "endCursor": "abc"},
            }
        }
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
        from wizsec._request import _RequestBase

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
        from wizsec._request import _RequestBase

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


def _page(nodes, *, has_next=True, cursor="cur-1", total=99):
    return {
        "users": {
            "nodes": list(nodes),
            "totalCount": total,
            "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
        }
    }


class TestStopPredicates:
    """Test _connection_key / _call_stop_predicate / _apply_stop_predicates."""

    def _make_base(self, stop_when=None, stop_on_page=None, source="users", after=None):
        import logging

        from wizsec._request import _RequestBase

        obj = object.__new__(_RequestBase)
        obj._logger = logging.getLogger("wizsec.test")
        obj._aggregated_data = None
        obj.data = None
        obj.errors = []
        obj.error = None
        obj.vars = {"after": after} if after else {}
        obj._query = "query Q { users { nodes { id } } }"
        obj._current_query_info = {"source": source}
        obj._stop_when = stop_when
        obj._stop_on_page = stop_on_page
        obj._stopped_early = False
        obj._stop_reason = None
        obj._stop_page = 0
        obj._stop_cursor = None
        obj._stop_next_cursor = None
        obj._pages_scanned = 0
        return obj

    # ── _connection_key ───────────────────────────────────────────────

    def test_connection_key_prefers_query_source(self):
        base = self._make_base(source="users")
        data = {"other": {"nodes": []}, "users": {"nodes": []}}
        assert base._connection_key(data) == "users"

    def test_connection_key_falls_back_to_first_nodes_field(self):
        base = self._make_base(source="missing")
        assert base._connection_key({"issues": {"nodes": []}}) == "issues"

    def test_connection_key_none_without_nodes(self):
        base = self._make_base()
        assert base._connection_key({"createReport": {"report": {"id": "1"}}}) is None
        assert base._connection_key(None) is None

    # ── no predicates ─────────────────────────────────────────────────

    def test_no_predicates_returns_identical_object(self):
        base = self._make_base()
        page = _page([{"id": "a"}])
        assert base._apply_stop_predicates(page) is page
        assert base._stopped_early is False
        assert base._pages_scanned == 0

    def test_none_page_data_passes_through(self):
        base = self._make_base(stop_when=lambda n: True)
        assert base._apply_stop_predicates(None) is None

    # ── stop_when ─────────────────────────────────────────────────────

    def test_stop_when_truncates_including_the_match(self):
        base = self._make_base(stop_when=lambda n: n["id"] == "c")
        page = _page([{"id": x} for x in "abcde"])
        result = base._apply_stop_predicates(page)

        assert result is not page
        assert [n["id"] for n in result["users"]["nodes"]] == ["a", "b", "c"]
        # the original page is untouched
        assert len(page["users"]["nodes"]) == 5
        assert base._stopped_early is True
        assert base._stop_reason == "node"

    def test_truncated_copy_keeps_page_info_and_server_total_count(self):
        base = self._make_base(stop_when=lambda n: n["id"] == "a")
        result = base._apply_stop_predicates(_page([{"id": "a"}, {"id": "b"}]))
        assert result["users"]["pageInfo"] == {
            "hasNextPage": True,
            "endCursor": "cur-1",
        }
        # totalCount stays the server-side total; stopped_early is the signal
        # that the node list is partial.
        assert result["users"]["totalCount"] == 99

    def test_stop_when_matching_last_node_still_stops(self):
        base = self._make_base(stop_when=lambda n: n["id"] == "b")
        result = base._apply_stop_predicates(_page([{"id": "a"}, {"id": "b"}]))
        assert [n["id"] for n in result["users"]["nodes"]] == ["a", "b"]
        assert base._stopped_early is True

    def test_no_match_returns_page_unchanged(self):
        base = self._make_base(stop_when=lambda n: False, stop_on_page=lambda p: False)
        page = _page([{"id": "a"}])
        assert base._apply_stop_predicates(page) is page
        assert base._stopped_early is False

    def test_empty_nodes_does_not_stop(self):
        base = self._make_base(stop_when=lambda n: True)
        page = {"users": {"pageInfo": {"hasNextPage": False, "endCursor": None}}}
        assert base._apply_stop_predicates(page) is page
        assert base._stopped_early is False

    # ── stop_on_page ──────────────────────────────────────────────────

    def test_stop_on_page_keeps_whole_page(self):
        base = self._make_base(stop_on_page=lambda p: True)
        page = _page([{"id": x} for x in "abc"])
        assert base._apply_stop_predicates(page) is page
        assert base._stopped_early is True
        assert base._stop_reason == "page"

    def test_stop_on_page_sees_untruncated_page_and_node_reason_wins(self):
        from unittest.mock import MagicMock

        on_page = MagicMock(return_value=False)
        base = self._make_base(stop_when=lambda n: n["id"] == "a", stop_on_page=on_page)
        page = _page([{"id": x} for x in "abcde"])
        result = base._apply_stop_predicates(page)

        on_page.assert_called_once()
        assert len(on_page.call_args[0][0]["users"]["nodes"]) == 5
        assert [n["id"] for n in result["users"]["nodes"]] == ["a"]
        assert base._stop_reason == "node"

    # ── bookkeeping ───────────────────────────────────────────────────

    def test_records_cursors_and_page_number(self):
        base = self._make_base(stop_when=lambda n: n["id"] == "c", after="cur-0")
        base._apply_stop_predicates(_page([{"id": "a"}], cursor="cur-1"))
        base.vars["after"] = "cur-1"
        base._apply_stop_predicates(_page([{"id": "b"}], cursor="cur-2"))
        base.vars["after"] = "cur-2"
        base._apply_stop_predicates(_page([{"id": "c"}], cursor="cur-3"))

        assert base._pages_scanned == 3
        assert base._stop_page == 3
        assert base._stop_cursor == "cur-2"  # the cursor that FETCHED page 3
        assert base._stop_next_cursor == "cur-3"
        assert base.stop_info == {
            "reason": "node",
            "page": 3,
            "cursor": "cur-2",
            "next_cursor": "cur-3",
        }

    def test_stop_info_none_when_not_stopped(self):
        base = self._make_base()
        assert base.stop_info is None

    # ── raising predicates ────────────────────────────────────────────

    def test_raising_stop_when_records_query_error(self):
        from wizsec.exceptions import WizQueryError

        def boom(node):
            raise ValueError("bad predicate")

        base = self._make_base(stop_when=boom)
        page = _page([{"id": "a"}])
        result = base._apply_stop_predicates(page)  # must not raise

        assert result is page
        assert base._stopped_early is False
        assert base.errors
        assert isinstance(base.error, WizQueryError)
        assert isinstance(base.error.original_error, ValueError)
        assert base.success() is False

    def test_raising_stop_on_page_records_query_error(self):
        from wizsec.exceptions import WizQueryError

        def boom(page_data):
            raise RuntimeError("nope")

        base = self._make_base(stop_on_page=boom)
        base._apply_stop_predicates(_page([{"id": "a"}]))

        assert base._stopped_early is False
        assert isinstance(base.error, WizQueryError)
