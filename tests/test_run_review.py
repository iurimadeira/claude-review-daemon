import json
import subprocess
from unittest.mock import MagicMock, call, mock_open, patch

import pytest

import run_review
from run_review import (
    ClaudeResult,
    MAX_COMMENT_LENGTH,
    ReviewOutput,
    _create_comment,
    create_review,
    fetch_existing_review_comments,
    find_existing_comments,
    format_review_as_comment,
    main,
    minimize_comment,
    parse_review_json,
    parse_stream_json,
    run,
    run_review as do_review,
    select_review_output,
    truncate_output,
    post_comment,
)

from tests.helpers import FROZEN_NOW, make_completed_process, make_mock_popen


# ---------------------------------------------------------------------------
# truncate_output
# ---------------------------------------------------------------------------

class TestTruncateOutput:
    def test_short_output_unchanged(self):
        assert truncate_output("hello") == "hello"

    def test_exact_limit_unchanged(self):
        text = "x" * MAX_COMMENT_LENGTH
        assert truncate_output(text) == text

    def test_long_output_truncated_with_notice(self):
        text = "x" * (MAX_COMMENT_LENGTH + 100)
        result = truncate_output(text)
        assert result.endswith("*Output truncated (exceeded GitHub comment limit)*")
        assert len(result) <= MAX_COMMENT_LENGTH

    def test_result_length_always_within_limit(self):
        for length in [MAX_COMMENT_LENGTH + 1, MAX_COMMENT_LENGTH + 10000]:
            result = truncate_output("z" * length)
            assert len(result) <= MAX_COMMENT_LENGTH


# ---------------------------------------------------------------------------
# find_existing_comments
# ---------------------------------------------------------------------------

class TestFindExistingComments:
    @patch("run_review.subprocess.run")
    def test_comments_found(self, mock_run):
        mock_run.return_value = make_completed_process(
            stdout='[{"id": 12345, "node_id": "IC_abc"}]\n'
        )
        result = find_existing_comments("owner/repo", 1, "review-pr")
        assert result == [{"id": 12345, "node_id": "IC_abc"}]

    @patch("run_review.subprocess.run")
    def test_multiple_comments_found(self, mock_run):
        mock_run.return_value = make_completed_process(
            stdout='[{"id": 100, "node_id": "IC_a"}, {"id": 200, "node_id": "IC_b"}]\n'
        )
        result = find_existing_comments("owner/repo", 1, "review-pr")
        assert len(result) == 2

    @patch("run_review.subprocess.run")
    def test_empty_stdout_returns_empty_list(self, mock_run):
        mock_run.return_value = make_completed_process(stdout="")
        assert find_existing_comments("owner/repo", 1, "review-pr") == []

    @patch("run_review.subprocess.run")
    def test_nonzero_returncode_returns_empty_list(self, mock_run):
        mock_run.return_value = make_completed_process(returncode=1, stdout="[]")
        assert find_existing_comments("owner/repo", 1, "review-pr") == []

    @patch("run_review.subprocess.run")
    def test_exception_returns_empty_list(self, mock_run):
        mock_run.side_effect = OSError("boom")
        assert find_existing_comments("owner/repo", 1, "review-pr") == []

    @patch("run_review.subprocess.run")
    def test_command_includes_marker_in_jq(self, mock_run):
        mock_run.return_value = make_completed_process(stdout="")
        find_existing_comments("owner/repo", 7, "custom-skill")
        args = mock_run.call_args[0][0]
        assert "gh" in args
        assert "/repos/owner/repo/issues/7/comments" in args
        jq_arg = [a for a in args if "select(" in a][0]
        assert "<!-- claude-review-daemon:custom-skill -->" in jq_arg


# ---------------------------------------------------------------------------
# minimize_comment
# ---------------------------------------------------------------------------

class TestMinimizeComment:
    @patch("run_review.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = make_completed_process()
        assert minimize_comment("IC_abc") is True
        args = mock_run.call_args[0][0]
        assert "graphql" in args
        assert any("IC_abc" in str(a) for a in args)

    @patch("run_review.subprocess.run")
    def test_failure_returns_false(self, mock_run):
        mock_run.return_value = make_completed_process(returncode=1, stderr="err")
        assert minimize_comment("IC_abc") is False

    @patch("run_review.subprocess.run")
    def test_exception_returns_false(self, mock_run):
        mock_run.side_effect = OSError("boom")
        assert minimize_comment("IC_abc") is False


# ---------------------------------------------------------------------------
# _create_comment
# ---------------------------------------------------------------------------

class TestCreateComment:
    @patch("run_review.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = make_completed_process()
        _create_comment("owner/repo", 5, "body text")
        args = mock_run.call_args[0][0]
        assert args[:2] == ["gh", "pr"]
        assert "5" in args
        assert "--repo" in args
        assert "body text" in args

    @patch("run_review.subprocess.run")
    def test_failure_logs_no_exception(self, mock_run):
        mock_run.return_value = make_completed_process(returncode=1, stderr="err")
        _create_comment("owner/repo", 5, "body")  # should not raise


# ---------------------------------------------------------------------------
# post_comment
# ---------------------------------------------------------------------------

class TestPostComment:
    @patch("run_review._create_comment")
    @patch("run_review.minimize_comment")
    @patch("run_review.find_existing_comments", return_value=[])
    def test_no_existing_creates_new(self, mock_find, mock_minimize, mock_create, frozen_now):
        post_comment("owner/repo", 1, "review output", "review-pr", "abc1234def")
        mock_minimize.assert_not_called()
        mock_create.assert_called_once()
        body = mock_create.call_args[0][2]
        assert "<!-- claude-review-daemon:review-pr -->" in body
        assert "review output" in body

    @patch("run_review._create_comment")
    @patch("run_review.minimize_comment", return_value=True)
    @patch("run_review.find_existing_comments")
    def test_minimizes_old_comments_before_creating(self, mock_find, mock_minimize, mock_create, frozen_now):
        mock_find.return_value = [
            {"id": 100, "node_id": "IC_a"},
            {"id": 200, "node_id": "IC_b"},
        ]
        post_comment("owner/repo", 1, "new review", "review-pr")
        assert mock_minimize.call_count == 2
        mock_minimize.assert_any_call("IC_a")
        mock_minimize.assert_any_call("IC_b")
        mock_create.assert_called_once()

    @patch("run_review._create_comment")
    @patch("run_review.minimize_comment", return_value=False)
    @patch("run_review.find_existing_comments")
    def test_minimize_failure_does_not_block_create(self, mock_find, mock_minimize, mock_create, frozen_now):
        mock_find.return_value = [{"id": 100, "node_id": "IC_a"}]
        post_comment("owner/repo", 1, "body", "review-pr")
        mock_create.assert_called_once()

    @patch("run_review._create_comment")
    @patch("run_review.find_existing_comments", return_value=[])
    def test_footer_includes_sha_when_provided(self, mock_find, mock_create, frozen_now):
        post_comment("owner/repo", 1, "body", "review-pr", "abc1234def5678")
        body = mock_create.call_args[0][2]
        assert "`abc1234`" in body

    @patch("run_review._create_comment")
    @patch("run_review.find_existing_comments", return_value=[])
    def test_footer_timestamp_only_when_no_sha(self, mock_find, mock_create, frozen_now):
        post_comment("owner/repo", 1, "body", "review-pr", None)
        body = mock_create.call_args[0][2]
        assert "Reviewed commit" not in body
        assert "2025-06-15 12:00 UTC" in body

    @patch("run_review._create_comment")
    @patch("run_review.find_existing_comments", return_value=[])
    def test_long_body_truncated(self, mock_find, mock_create, frozen_now):
        post_comment("owner/repo", 1, "x" * (MAX_COMMENT_LENGTH + 500), "review-pr")
        body = mock_create.call_args[0][2]
        assert len(body) <= MAX_COMMENT_LENGTH


# ---------------------------------------------------------------------------
# fetch_existing_review_comments
# ---------------------------------------------------------------------------

class TestFetchExistingReviewComments:
    @patch("run_review.subprocess.run")
    def test_returns_comments(self, mock_run):
        mock_run.return_value = make_completed_process(
            stdout='[{"path": "a.py", "line": 10, "body": "Fix this"}]\n'
        )
        result = fetch_existing_review_comments("owner/repo", 1)
        assert result == [{"path": "a.py", "line": 10, "body": "Fix this"}]
        cmd = mock_run.call_args[0][0]
        assert "/repos/owner/repo/pulls/1/comments" in cmd

    @patch("run_review.subprocess.run")
    def test_empty_returns_empty_list(self, mock_run):
        mock_run.return_value = make_completed_process(stdout="")
        assert fetch_existing_review_comments("owner/repo", 1) == []

    @patch("run_review.subprocess.run")
    def test_error_returns_empty_list(self, mock_run):
        mock_run.return_value = make_completed_process(returncode=1)
        assert fetch_existing_review_comments("owner/repo", 1) == []

    @patch("run_review.subprocess.run")
    def test_exception_returns_empty_list(self, mock_run):
        mock_run.side_effect = OSError("boom")
        assert fetch_existing_review_comments("owner/repo", 1) == []


# ---------------------------------------------------------------------------
# run wrapper
# ---------------------------------------------------------------------------

class TestRunWrapper:
    @patch("run_review.subprocess.run")
    def test_passes_expected_args(self, mock_run):
        mock_run.return_value = make_completed_process()
        run(["echo", "hi"], cwd="/tmp", capture=True)
        mock_run.assert_called_once_with(
            ["echo", "hi"], cwd="/tmp", capture_output=True, text=True, timeout=1800,
        )

    @patch("run_review.subprocess.run")
    def test_propagates_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="x", timeout=1800)
        with pytest.raises(subprocess.TimeoutExpired):
            run(["sleep", "9999"])


# ---------------------------------------------------------------------------
# parse_stream_json
# ---------------------------------------------------------------------------

def _assistant_event(text):
    return json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})

def _result_event(result="", cost_usd=None, num_turns=None, session_id=None, subtype=None):
    e = {"type": "result", "result": result}
    if cost_usd is not None:
        e["cost_usd"] = cost_usd
    if num_turns is not None:
        e["num_turns"] = num_turns
    if session_id is not None:
        e["session_id"] = session_id
    if subtype is not None:
        e["subtype"] = subtype
    return json.dumps(e)


class TestParseStreamJson:
    def test_single_assistant_and_result(self):
        lines = [_assistant_event("Hello review"), _result_event("Final result", cost_usd=0.05, num_turns=3, session_id="s1")]
        r = parse_stream_json(iter(lines))
        assert r.result_text == "Final result"
        assert r.all_assistant_text == ["Hello review"]
        assert r.cost_usd == 0.05
        assert r.num_turns == 3
        assert r.session_id == "s1"
        assert r.is_error is False

    def test_multi_turn_only_text_blocks(self):
        tool_event = json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "read"}]}})
        lines = [_assistant_event("First"), tool_event, _assistant_event("Second"), _result_event("done")]
        r = parse_stream_json(iter(lines))
        assert r.all_assistant_text == ["First", "Second"]

    def test_malformed_lines_skipped(self):
        lines = ["not json", _assistant_event("ok"), "{bad json", _result_event("done")]
        r = parse_stream_json(iter(lines))
        assert r.all_assistant_text == ["ok"]
        assert r.result_text == "done"

    def test_empty_stream(self):
        r = parse_stream_json(iter([]))
        assert r.result_text == ""
        assert r.all_assistant_text == []
        assert r.cost_usd is None
        assert r.is_error is False

    def test_error_result(self):
        lines = [_result_event("err msg", subtype="error")]
        r = parse_stream_json(iter(lines))
        assert r.is_error is True
        assert r.result_text == "err msg"

    def test_blank_lines_ignored(self):
        lines = ["", "  ", _assistant_event("text"), "", _result_event("done")]
        r = parse_stream_json(iter(lines))
        assert r.all_assistant_text == ["text"]

    def test_empty_text_blocks_ignored(self):
        event = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "  "}]}})
        lines = [event, _assistant_event("real"), _result_event("done")]
        r = parse_stream_json(iter(lines))
        assert r.all_assistant_text == ["real"]


# ---------------------------------------------------------------------------
# select_review_output
# ---------------------------------------------------------------------------

class TestSelectReviewOutput:
    def test_substantial_result_used_as_is(self):
        r = ClaudeResult(result_text="## Review\nLooks good!", all_assistant_text=["msg1"], cost_usd=None, num_turns=None, is_error=False, session_id=None)
        assert select_review_output(r) == "## Review\nLooks good!"

    def test_stub_detected_falls_back(self):
        r = ClaudeResult(result_text="Review complete. All findings are in the consolidated report above.", all_assistant_text=["## Review\nBig report here"], cost_usd=None, num_turns=None, is_error=False, session_id=None)
        assert select_review_output(r) == "## Review\nBig report here"

    def test_empty_result_uses_assistant_text(self):
        r = ClaudeResult(result_text="", all_assistant_text=["msg1", "msg2"], cost_usd=None, num_turns=None, is_error=False, session_id=None)
        assert select_review_output(r) == "msg1\n\nmsg2"

    def test_short_text_without_stub_patterns(self):
        r = ClaudeResult(result_text="LGTM, no issues found.", all_assistant_text=["other"], cost_usd=None, num_turns=None, is_error=False, session_id=None)
        assert select_review_output(r) == "LGTM, no issues found."

    def test_both_empty_returns_empty(self):
        r = ClaudeResult(result_text="", all_assistant_text=[], cost_usd=None, num_turns=None, is_error=False, session_id=None)
        assert select_review_output(r) == ""

    def test_stub_without_assistant_text_uses_result(self):
        r = ClaudeResult(result_text="Review complete.", all_assistant_text=[], cost_usd=None, num_turns=None, is_error=False, session_id=None)
        assert select_review_output(r) == "Review complete."

    def test_long_text_with_stub_pattern_not_triggered(self):
        long_review = "Review complete. " + "x" * 600
        r = ClaudeResult(result_text=long_review, all_assistant_text=["other"], cost_usd=None, num_turns=None, is_error=False, session_id=None)
        assert select_review_output(r) == long_review


# ---------------------------------------------------------------------------
# parse_review_json
# ---------------------------------------------------------------------------

class TestParseReviewJson:
    def test_valid_json(self):
        text = json.dumps({
            "summary": "Looks good overall",
            "event": "APPROVE",
            "comments": [{"path": "src/main.py", "line": 10, "body": "Nice work"}],
        })
        result = parse_review_json(text)
        assert result is not None
        assert result.summary == "Looks good overall"
        assert result.event == "APPROVE"
        assert result.comments == [{"path": "src/main.py", "line": 10, "body": "Nice work"}]

    def test_json_in_code_fence(self):
        text = '```json\n{"summary": "LGTM", "event": "APPROVE", "comments": []}\n```'
        result = parse_review_json(text)
        assert result is not None
        assert result.summary == "LGTM"

    def test_json_with_surrounding_text(self):
        text = 'Here is my review:\n{"summary": "Found issues", "event": "REQUEST_CHANGES", "comments": []}\nDone.'
        result = parse_review_json(text)
        assert result is not None
        assert result.event == "REQUEST_CHANGES"

    def test_invalid_json_returns_none(self):
        assert parse_review_json("not json at all") is None

    def test_missing_summary_returns_none(self):
        text = json.dumps({"event": "COMMENT", "comments": []})
        assert parse_review_json(text) is None

    def test_empty_summary_returns_none(self):
        text = json.dumps({"summary": "  ", "event": "COMMENT", "comments": []})
        assert parse_review_json(text) is None

    def test_invalid_event_coerced_to_request_changes(self):
        text = json.dumps({"summary": "Review", "event": "INVALID", "comments": []})
        result = parse_review_json(text)
        assert result is not None
        assert result.event == "REQUEST_CHANGES"

    def test_missing_event_defaults_to_request_changes(self):
        text = json.dumps({"summary": "Review", "comments": []})
        result = parse_review_json(text)
        assert result is not None
        assert result.event == "REQUEST_CHANGES"

    def test_comment_event_coerced_to_request_changes(self):
        text = json.dumps({"summary": "Review", "event": "COMMENT", "comments": []})
        result = parse_review_json(text)
        assert result is not None
        assert result.event == "REQUEST_CHANGES"

    def test_comments_with_missing_fields_filtered(self):
        text = json.dumps({
            "summary": "Review",
            "event": "COMMENT",
            "comments": [
                {"path": "a.py", "line": 1, "body": "ok"},
                {"path": "b.py", "line": 2},  # missing body
                {"path": "c.py", "body": "no line"},  # missing line
                {"body": "no path"},  # missing path
                {"path": "d.py", "line": 3, "body": "also ok"},
            ],
        })
        result = parse_review_json(text)
        assert result is not None
        assert len(result.comments) == 2
        assert result.comments[0]["path"] == "a.py"
        assert result.comments[1]["path"] == "d.py"

    def test_empty_text_returns_none(self):
        assert parse_review_json("") is None

    def test_no_braces_returns_none(self):
        assert parse_review_json("just plain text without braces") is None

    def test_null_line_filtered_out(self):
        text = json.dumps({
            "summary": "Review",
            "event": "REQUEST_CHANGES",
            "comments": [
                {"path": "a.py", "line": None, "body": "no line"},
                {"path": "b.py", "line": 10, "body": "has line"},
            ],
        })
        result = parse_review_json(text)
        assert len(result.comments) == 1
        assert result.comments[0]["path"] == "b.py"

    def test_zero_or_negative_line_filtered_out(self):
        text = json.dumps({
            "summary": "Review",
            "event": "APPROVE",
            "comments": [
                {"path": "a.py", "line": 0, "body": "zero"},
                {"path": "b.py", "line": -1, "body": "negative"},
                {"path": "c.py", "line": 1, "body": "valid"},
            ],
        })
        result = parse_review_json(text)
        assert len(result.comments) == 1
        assert result.comments[0]["path"] == "c.py"

    def test_line_coerced_to_int(self):
        text = json.dumps({
            "summary": "Review",
            "event": "COMMENT",
            "comments": [{"path": "a.py", "line": "42", "body": "ok"}],
        })
        result = parse_review_json(text)
        assert result.comments[0]["line"] == 42


# ---------------------------------------------------------------------------
# format_review_as_comment
# ---------------------------------------------------------------------------

class TestFormatReviewAsComment:
    def test_summary_only(self):
        review = ReviewOutput(summary="All good", event="APPROVE", comments=[])
        assert format_review_as_comment(review) == "All good"

    def test_with_comments(self):
        review = ReviewOutput(
            summary="Found issues",
            event="REQUEST_CHANGES",
            comments=[
                {"path": "src/main.py", "line": 10, "body": "Fix this"},
                {"path": "src/utils.py", "line": 25, "body": "Also fix this"},
            ],
        )
        result = format_review_as_comment(review)
        assert "Found issues" in result
        assert "**`src/main.py` (line 10)**" in result
        assert "Fix this" in result
        assert "**`src/utils.py` (line 25)**" in result


# ---------------------------------------------------------------------------
# create_review
# ---------------------------------------------------------------------------

class TestCreateReview:
    @patch("run_review.subprocess.run")
    def test_success(self, mock_run, frozen_now):
        mock_run.return_value = make_completed_process(
            stdout='{"html_url": "https://github.com/owner/repo/pull/1#pullrequestreview-123"}'
        )
        review = ReviewOutput(
            summary="LGTM",
            event="APPROVE",
            comments=[{"path": "a.py", "line": 10, "body": "Nice"}],
        )
        url = create_review("owner/repo", 1, "abc1234def5678", review, "review-pr")
        assert url == "https://github.com/owner/repo/pull/1#pullrequestreview-123"

    @patch("run_review.subprocess.run")
    def test_payload_structure(self, mock_run, frozen_now):
        mock_run.return_value = make_completed_process(stdout='{"html_url": "https://example.com"}')
        review = ReviewOutput(
            summary="Review",
            event="REQUEST_CHANGES",
            comments=[{"path": "b.py", "line": 5, "body": "Issue"}],
        )
        create_review("owner/repo", 42, "deadbeef12345678", review, "review-pr")
        args = mock_run.call_args
        cmd = args[0][0]
        assert "repos/owner/repo/pulls/42/reviews" in cmd
        assert "--method" in cmd
        assert "POST" in cmd
        assert "--input" in cmd
        payload = json.loads(args[1]["input"])
        assert payload["commit_id"] == "deadbeef12345678"
        assert payload["event"] == "REQUEST_CHANGES"
        assert len(payload["comments"]) == 1
        assert payload["comments"][0]["side"] == "RIGHT"
        assert "<!-- claude-review-daemon:review-pr -->" in payload["body"]

    @patch("run_review.subprocess.run")
    def test_failure_returns_none(self, mock_run, frozen_now):
        mock_run.return_value = make_completed_process(returncode=1, stderr="error")
        review = ReviewOutput(summary="Review", event="REQUEST_CHANGES", comments=[])
        assert create_review("owner/repo", 1, "abc123", review, "review-pr") is None

    @patch("run_review.subprocess.run")
    def test_invalid_json_response(self, mock_run, frozen_now):
        mock_run.return_value = make_completed_process(stdout="not json")
        review = ReviewOutput(summary="Review", event="REQUEST_CHANGES", comments=[])
        assert create_review("owner/repo", 1, "abc123", review, "review-pr") is None


# ---------------------------------------------------------------------------
# run_review orchestration
# ---------------------------------------------------------------------------

class TestRunReviewOrchestration:
    COMMON_KWARGS = dict(
        repo="owner/repo",
        pr_number=42,
        branch="feature",
        base_branch="main",
        skill="review-pr",
        repo_dir="/repos",
        head_sha="abc1234def5678",
    )

    REVIEW_JSON = json.dumps({
        "summary": "Looks good overall",
        "event": "APPROVE",
        "comments": [{"path": "src/main.py", "line": 10, "body": "Nice work"}],
    })

    @staticmethod
    def _stream_lines(result_text="Review result", assistant_texts=None, cost_usd=0.01, num_turns=2):
        lines = []
        for text in (assistant_texts or []):
            lines.append(_assistant_event(text))
        lines.append(_result_event(result_text, cost_usd=cost_usd, num_turns=num_turns, session_id="s1"))
        return lines

    @patch("run_review.notify_review_posted")
    @patch("run_review.create_review", return_value="https://github.com/owner/repo/pull/42#review")
    @patch("run_review.find_existing_comments", return_value=[])
    @patch("run_review.fetch_existing_review_comments", return_value=[])
    @patch("run_review.subprocess.Popen")
    @patch("run_review.run")
    @patch("run_review.os.path.exists", return_value=False)
    @patch("run_review.os.path.isfile", return_value=True)
    @patch("builtins.open", mock_open(read_data="skill content"))
    def test_happy_path_review(self, mock_isfile, mock_exists, mock_run_wrap, mock_popen,
                                mock_fetch, mock_find, mock_create, mock_notify):
        mock_popen.return_value = make_mock_popen(stdout_lines=self._stream_lines(self.REVIEW_JSON))
        do_review(**self.COMMON_KWARGS)
        mock_create.assert_called_once()
        assert mock_create.call_args[0][2] == "abc1234def5678"  # head_sha
        mock_notify.assert_called_once()
        assert "Looks good overall" in mock_notify.call_args[0][2]

    @patch("run_review.notify_review_posted")
    @patch("run_review.create_review", return_value=None)
    @patch("run_review.post_comment", return_value="https://example.com")
    @patch("run_review.find_existing_comments", return_value=[])
    @patch("run_review.fetch_existing_review_comments", return_value=[])
    @patch("run_review.subprocess.Popen")
    @patch("run_review.run")
    @patch("run_review.os.path.exists", return_value=False)
    @patch("run_review.os.path.isfile", return_value=True)
    @patch("builtins.open", mock_open(read_data="skill"))
    def test_review_creation_failure_falls_back_to_comment(
        self, mock_isfile, mock_exists, mock_run_wrap, mock_popen,
        mock_fetch, mock_find, mock_post, mock_create, mock_notify,
    ):
        mock_popen.return_value = make_mock_popen(stdout_lines=self._stream_lines(self.REVIEW_JSON))
        do_review(**self.COMMON_KWARGS)
        mock_create.assert_called_once()
        mock_post.assert_called_once()
        body = mock_post.call_args[0][2]
        assert "Looks good overall" in body
        assert "src/main.py" in body

    @patch("run_review.post_comment")
    @patch("run_review.fetch_existing_review_comments", return_value=[])
    @patch("run_review.subprocess.Popen")
    @patch("run_review.run")
    @patch("run_review.os.path.exists", return_value=False)
    @patch("run_review.os.path.isfile", return_value=True)
    @patch("builtins.open", mock_open(read_data="skill content"))
    def test_non_json_output_uses_post_comment(self, mock_isfile, mock_exists, mock_run_wrap, mock_popen, mock_fetch, mock_post):
        mock_popen.return_value = make_mock_popen(stdout_lines=self._stream_lines("Review result"))
        do_review(**self.COMMON_KWARGS)
        mock_post.assert_called_once()
        assert "Review result" in mock_post.call_args[0][2]

    @patch("run_review.notify_review_posted")
    @patch("run_review.minimize_comment", return_value=True)
    @patch("run_review.find_existing_comments")
    @patch("run_review.create_review", return_value="https://example.com")
    @patch("run_review.fetch_existing_review_comments", return_value=[])
    @patch("run_review.subprocess.Popen")
    @patch("run_review.run")
    @patch("run_review.os.path.exists", return_value=False)
    @patch("run_review.os.path.isfile", return_value=True)
    @patch("builtins.open", mock_open(read_data="skill"))
    def test_review_path_minimizes_old_comments(
        self, mock_isfile, mock_exists, mock_run_wrap, mock_popen,
        mock_fetch, mock_create, mock_find, mock_minimize, mock_notify,
    ):
        mock_find.return_value = [{"id": 100, "node_id": "IC_a"}]
        mock_popen.return_value = make_mock_popen(stdout_lines=self._stream_lines(self.REVIEW_JSON))
        do_review(**self.COMMON_KWARGS)
        mock_minimize.assert_called_once_with("IC_a")
        mock_create.assert_called_once()

    @patch("run_review.post_comment")
    @patch("run_review.fetch_existing_review_comments", return_value=[])
    @patch("run_review.subprocess.Popen")
    @patch("run_review.run")
    @patch("run_review.os.path.exists", return_value=True)
    @patch("run_review.os.path.isfile", return_value=True)
    @patch("builtins.open", mock_open(read_data="skill"))
    def test_stale_worktree_removed(self, mock_isfile, mock_exists, mock_run_wrap, mock_popen, mock_fetch, mock_post):
        mock_popen.return_value = make_mock_popen(stdout_lines=self._stream_lines("ok"))
        do_review(**self.COMMON_KWARGS)
        remove_calls = [c for c in mock_run_wrap.call_args_list if "worktree" in str(c) and "remove" in str(c)]
        assert len(remove_calls) >= 1

    @patch("run_review.post_comment")
    @patch("run_review.fetch_existing_review_comments", return_value=[])
    @patch("run_review.subprocess.Popen")
    @patch("run_review.run")
    @patch("run_review.os.path.exists", return_value=False)
    @patch("run_review.os.path.isfile", return_value=True)
    @patch("builtins.open", mock_open(read_data="skill"))
    def test_no_head_sha_uses_post_comment(self, mock_isfile, mock_exists, mock_run_wrap, mock_popen, mock_fetch, mock_post):
        mock_popen.return_value = make_mock_popen(stdout_lines=self._stream_lines(self.REVIEW_JSON))
        kwargs = {**self.COMMON_KWARGS, "head_sha": None}
        do_review(**kwargs)
        mock_post.assert_called_once()

    @patch("run_review.post_comment")
    @patch("run_review.run")
    @patch("run_review.os.path.exists", return_value=False)
    @patch("run_review.os.path.isfile", return_value=False)
    def test_skill_not_found(self, mock_isfile, mock_exists, mock_run_wrap, mock_post):
        do_review(**self.COMMON_KWARGS)
        body = mock_post.call_args[0][2]
        assert "Skill file not found" in body

    @patch("run_review.post_comment")
    @patch("run_review.fetch_existing_review_comments", return_value=[])
    @patch("run_review.subprocess.Popen")
    @patch("run_review.run")
    @patch("run_review.os.path.exists", return_value=False)
    @patch("run_review.os.path.isfile", return_value=True)
    @patch("builtins.open", mock_open(read_data="skill"))
    def test_claude_nonzero_exit(self, mock_isfile, mock_exists, mock_run_wrap, mock_popen, mock_fetch, mock_post):
        mock_popen.return_value = make_mock_popen(
            stdout_lines=self._stream_lines("partial"),
            returncode=1,
            stderr="error detail",
        )
        do_review(**self.COMMON_KWARGS)
        body = mock_post.call_args[0][2]
        assert "partial" in body or "error detail" in body

    @patch("run_review.post_comment")
    @patch("run_review.fetch_existing_review_comments", return_value=[])
    @patch("run_review.subprocess.Popen")
    @patch("run_review.run")
    @patch("run_review.os.path.exists", return_value=False)
    @patch("run_review.os.path.isfile", return_value=True)
    @patch("builtins.open", mock_open(read_data="skill"))
    def test_claude_empty_output(self, mock_isfile, mock_exists, mock_run_wrap, mock_popen, mock_fetch, mock_post):
        mock_popen.return_value = make_mock_popen(stdout_lines=[_result_event("")])
        do_review(**self.COMMON_KWARGS)
        body = mock_post.call_args[0][2]
        assert "produced no output" in body

    @patch("run_review.post_comment")
    @patch("run_review.fetch_existing_review_comments", return_value=[])
    @patch("run_review.subprocess.Popen")
    @patch("run_review.run")
    @patch("run_review.os.path.exists", return_value=False)
    @patch("run_review.os.path.isfile", return_value=True)
    @patch("builtins.open", mock_open(read_data="skill"))
    def test_timeout(self, mock_isfile, mock_exists, mock_run_wrap, mock_popen, mock_fetch, mock_post):
        mock_proc = make_mock_popen(
            stdout_lines=[],
            wait_side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=600),
        )
        mock_popen.return_value = mock_proc
        do_review(**self.COMMON_KWARGS)
        body = mock_post.call_args[0][2]
        assert "timed out" in body

    @patch("run_review.post_comment")
    @patch("run_review.fetch_existing_review_comments", return_value=[])
    @patch("run_review.subprocess.Popen")
    @patch("run_review.run")
    @patch("run_review.os.path.exists", return_value=False)
    @patch("run_review.os.path.isfile", return_value=True)
    @patch("builtins.open", mock_open(read_data="skill"))
    def test_generic_exception(self, mock_isfile, mock_exists, mock_run_wrap, mock_popen, mock_fetch, mock_post):
        mock_popen.side_effect = RuntimeError("unexpected")
        do_review(**self.COMMON_KWARGS)
        body = mock_post.call_args[0][2]
        assert "RuntimeError" in body

    @patch("run_review.post_comment")
    @patch("run_review.run")
    @patch("run_review.os.path.exists")
    @patch("run_review.os.path.isfile", return_value=False)
    def test_finally_always_cleans_up(self, mock_isfile, mock_exists_fn, mock_run_wrap, mock_post):
        mock_exists_fn.side_effect = [False, True]
        do_review(**self.COMMON_KWARGS)
        cleanup_calls = [c for c in mock_run_wrap.call_args_list if "worktree" in str(c) and "remove" in str(c)]
        assert len(cleanup_calls) >= 1

    @patch("run_review.post_comment")
    @patch("run_review.run")
    @patch("run_review.os.path.exists")
    @patch("run_review.os.path.isfile", return_value=False)
    def test_cleanup_failure_swallowed(self, mock_isfile, mock_exists_fn, mock_run_wrap, mock_post):
        mock_exists_fn.side_effect = [False, True]
        mock_run_wrap.side_effect = [
            make_completed_process(),  # git fetch
            make_completed_process(),  # worktree add
            OSError("cleanup fail"),   # worktree remove in finally
        ]
        do_review(**self.COMMON_KWARGS)  # should not raise


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

class TestMain:
    @patch("run_review.run_review")
    def test_all_args_provided(self, mock_rr):
        with patch("sys.argv", [
            "run_review.py",
            "--repo", "owner/repo",
            "--pr-number", "10",
            "--branch", "feat",
            "--base-branch", "main",
            "--skill", "custom",
            "--repo-dir", "/tmp/repos",
            "--head-sha", "deadbeef",
        ]):
            main()
        mock_rr.assert_called_once_with(
            repo="owner/repo",
            pr_number=10,
            branch="feat",
            base_branch="main",
            skill="custom",
            repo_dir="/tmp/repos",
            head_sha="deadbeef",
        )

    def test_missing_required_args(self):
        with patch("sys.argv", ["run_review.py"]):
            with pytest.raises(SystemExit):
                main()

    @patch("run_review.run_review")
    def test_skill_defaults(self, mock_rr):
        with patch("sys.argv", [
            "run_review.py",
            "--repo", "o/r", "--pr-number", "1",
            "--branch", "b", "--base-branch", "m",
            "--repo-dir", "/d",
        ]):
            main()
        assert mock_rr.call_args[1]["skill"] == "review-pr"

    @patch("run_review.run_review")
    def test_head_sha_defaults_none(self, mock_rr):
        with patch("sys.argv", [
            "run_review.py",
            "--repo", "o/r", "--pr-number", "1",
            "--branch", "b", "--base-branch", "m",
            "--repo-dir", "/d",
        ]):
            main()
        assert mock_rr.call_args[1]["head_sha"] is None
