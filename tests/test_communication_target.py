import pytest

from apps.shell.agent.runtime.communication_target import conversation_recipient_matches


def _header(recipient="张三", **changes):
    return {"role": "AXStaticText", "name": recipient, "value": recipient,
            "description": "Conversation header", "depth": 1, **changes}


def test_one_actual_conversation_header_matches_the_exact_recipient():
    assert conversation_recipient_matches({"elements": [_header()]}, "张三")
    assert conversation_recipient_matches({"title": "张三"}, "张三")
    heading = _header(role="AXHeading", description="")
    assert conversation_recipient_matches({"elements": [heading]}, "张三")


@pytest.mark.parametrize("data", [
    {}, {"conversation_recipient": "张三"},
    {"title": "张三", "elements": [_header("李四")]}, {"title": "张三 - Search Results"},
    {"elements": [_header("李四")]}, {"elements": [_header(description="visible text")]},
    {"elements": [_header(depth=None)]}, {"elements": [_header(depth=True)]},
    {"elements": [_header(), _header()]}, {"elements": [_header(), _header("李四")]},
    {"elements": [_header()], "truncated": True},
    {"elements": [{"role": "AXTable", "name": "Search Results", "depth": 0}, _header()]},
    {"elements": [{"role": "AXGroup", "name": "搜索结果", "depth": 0}, _header()]},
    {"elements": [{"role": "AXRow", "name": "张三", "depth": 1}]},
    {"elements": [_header(value="张三 ")]},
])
def test_search_rows_metadata_missing_ambiguous_or_different_headers_are_not_a_recipient(data):
    assert not conversation_recipient_matches(data, "张三")


def test_search_results_are_excluded_but_a_separate_top_level_header_is_accepted():
    data = {"elements": [
        {"role": "AXTable", "name": "Search Results", "depth": 1},
        _header(depth=2), _header(depth=1),
    ]}
    assert conversation_recipient_matches(data, "张三")


def test_no_explicit_recipient_does_not_invent_one():
    assert conversation_recipient_matches({}, "")
