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
    fetch_diff_lines,
    find_existing_comments,
    format_review_as_comment,
    main,
    minimize_comment,
    parse_review_json,
    parse_review_markdown,
    parse_stream_json,
    post_comment,
    run,
    run_review as do_review,
    select_review_output,
    truncate_output,
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
# parse_review_json
# ---------------------------------------------------------------------------

class TestParseReviewJson:
    def test_valid_json(self):
        text = '{"summary": "Looks good", "event": "APPROVE", "comments": [{"path": "a.py", "line": 10, "body": "nit"}]}'
        r = parse_review_json(text)
        assert r is not None
        assert r.summary == "Looks good"
        assert r.event == "APPROVE"
        assert len(r.comments) == 1
        assert r.comments[0] == {"path": "a.py", "line": 10, "body": "nit"}

    def test_json_in_code_fences(self):
        text = '```json\n{"summary": "OK", "event": "APPROVE", "comments": []}\n```'
        r = parse_review_json(text)
        assert r is not None
        assert r.summary == "OK"

    def test_invalid_json_returns_none(self):
        assert parse_review_json("not json at all") is None

    def test_missing_summary_returns_none(self):
        text = '{"event": "APPROVE", "comments": []}'
        assert parse_review_json(text) is None

    def test_empty_summary_returns_none(self):
        text = '{"summary": "  ", "event": "APPROVE", "comments": []}'
        assert parse_review_json(text) is None

    def test_invalid_event_defaults_to_request_changes(self):
        text = '{"summary": "Review", "event": "INVALID", "comments": []}'
        r = parse_review_json(text)
        assert r.event == "REQUEST_CHANGES"

    def test_missing_event_defaults_to_request_changes(self):
        text = '{"summary": "Review", "comments": []}'
        r = parse_review_json(text)
        assert r.event == "REQUEST_CHANGES"

    def test_comment_missing_path_filtered(self):
        text = '{"summary": "Review", "event": "APPROVE", "comments": [{"line": 1, "body": "x"}]}'
        r = parse_review_json(text)
        assert r.comments == []

    def test_comment_missing_body_filtered(self):
        text = '{"summary": "Review", "event": "APPROVE", "comments": [{"path": "a.py", "line": 1}]}'
        r = parse_review_json(text)
        assert r.comments == []

    def test_comment_invalid_line_filtered(self):
        text = '{"summary": "Review", "event": "APPROVE", "comments": [{"path": "a.py", "line": null, "body": "x"}]}'
        r = parse_review_json(text)
        assert r.comments == []

    def test_comment_zero_line_filtered(self):
        text = '{"summary": "Review", "event": "APPROVE", "comments": [{"path": "a.py", "line": 0, "body": "x"}]}'
        r = parse_review_json(text)
        assert r.comments == []

    def test_json_surrounded_by_text(self):
        text = 'Here is my review:\n{"summary": "OK", "event": "APPROVE", "comments": []}\nDone.'
        r = parse_review_json(text)
        assert r is not None
        assert r.summary == "OK"


# ---------------------------------------------------------------------------
# parse_review_markdown
# ---------------------------------------------------------------------------

class TestParseReviewMarkdown:
    def test_basic_markdown(self):
        text = (
            "Good code overall.\n\n"
            "**`src/main.py` (line 42)**\n"
            "This function is too long.\n\n"
            "**`src/utils.py` (line 10)**\n"
            "Unused import.\n"
        )
        r = parse_review_markdown(text)
        assert r is not None
        assert r.summary == "Good code overall."
        assert r.event == "REQUEST_CHANGES"
        assert len(r.comments) == 2
        assert r.comments[0]["path"] == "src/main.py"
        assert r.comments[0]["line"] == 42
        assert "too long" in r.comments[0]["body"]
        assert r.comments[1]["path"] == "src/utils.py"
        assert r.comments[1]["line"] == 10

    def test_no_findings_returns_none(self):
        assert parse_review_markdown("Just a plain review with no inline comments.") is None

    def test_html_comments_stripped_from_summary(self):
        text = (
            "<!-- claude-review-daemon:review-pr -->\n"
            "Summary here.\n\n"
            "**`a.py` (line 1)**\n"
            "Finding.\n"
        )
        r = parse_review_markdown(text)
        assert "<!--" not in r.summary
        assert "Summary here." in r.summary

    def test_line_singular_and_plural(self):
        text = (
            "Summary.\n\n"
            "**`a.py` (line 1)**\n"
            "First.\n\n"
            "**`b.py` (lines 5)**\n"
            "Second.\n"
        )
        r = parse_review_markdown(text)
        assert len(r.comments) == 2

    def test_empty_summary_gets_default(self):
        text = (
            "**`a.py` (line 1)**\n"
            "Finding.\n"
        )
        r = parse_review_markdown(text)
        assert "1 inline comment" in r.summary


# ---------------------------------------------------------------------------
# format_review_as_comment
# ---------------------------------------------------------------------------

class TestFormatReviewAsComment:
    def test_basic_format(self):
        review = ReviewOutput(
            summary="Good code.",
            event="APPROVE",
            comments=[{"path": "a.py", "line": 10, "body": "nit: spacing"}],
        )
        result = format_review_as_comment(review)
        assert "Good code." in result
        assert "**`a.py` (line 10)**" in result
        assert "nit: spacing" in result


# ---------------------------------------------------------------------------
# find_existing_comments
# ---------------------------------------------------------------------------

class TestFindExistingComments:
    @patch("run_review.subprocess.run")
    def test_comments_found(self, mock_run):
        mock_run.return_value = make_completed_process(
            stdout='[{"id": 123, "node_id": "MDEyOk"}, {"id": 456, "node_id": "MDEyOl"}]'
        )
        result = find_existing_comments("owner/repo", 1, "review-pr")
        assert len(result) == 2
        assert result[0]["id"] == 123
        assert result[0]["node_id"] == "MDEyOk"

    @patch("run_review.subprocess.run")
    def test_empty_returns_empty_list(self, mock_run):
        mock_run.return_value = make_completed_process(stdout="")
        assert find_existing_comments("owner/repo", 1, "review-pr") == []

    @patch("run_review.subprocess.run")
    def test_nonzero_returncode_returns_empty(self, mock_run):
        mock_run.return_value = make_completed_process(returncode=1)
        assert find_existing_comments("owner/repo", 1, "review-pr") == []

    @patch("run_review.subprocess.run")
    def test_exception_returns_empty(self, mock_run):
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
        assert minimize_comment("MDEyOk") is True

    @patch("run_review.subprocess.run")
    def test_failure(self, mock_run):
        mock_run.return_value = make_completed_process(returncode=1, stderr="err")
        assert minimize_comment("MDEyOk") is False

    @patch("run_review.subprocess.run")
    def test_exception(self, mock_run):
        mock_run.side_effect = OSError("boom")
        assert minimize_comment("MDEyOk") is False


# ---------------------------------------------------------------------------
# create_review
# ---------------------------------------------------------------------------

class TestFetchDiffLines:
    @patch("run_review.subprocess.run")
    def test_parses_diff(self, mock_run):
        mock_run.return_value = make_completed_process(
            stdout=json.dumps([
                {
                    "filename": "a.py",
                    "patch": "@@ -1,3 +1,5 @@\n line1\n+added1\n+added2\n line3\n line4",
                },
            ])
        )
        result = fetch_diff_lines("owner/repo", 1)
        assert "a.py" in result
        assert 2 in result["a.py"]  # +added1
        assert 3 in result["a.py"]  # +added2

    @patch("run_review.subprocess.run")
    def test_failure_returns_empty(self, mock_run):
        mock_run.return_value = make_completed_process(returncode=1)
        assert fetch_diff_lines("owner/repo", 1) == {}

    @patch("run_review.subprocess.run")
    def test_exception_returns_empty(self, mock_run):
        mock_run.side_effect = OSError("boom")
        assert fetch_diff_lines("owner/repo", 1) == {}


class TestCreateReview:
    @patch("run_review._submit_review")
    @patch("run_review.fetch_diff_lines")
    def test_success_with_valid_comments(self, mock_diff, mock_submit, frozen_now):
        mock_diff.return_value = {"a.py": {10, 11, 12}}
        mock_submit.return_value = "https://github.com/owner/repo/pull/1#review-123"
        review = ReviewOutput(
            summary="LGTM",
            event="APPROVE",
            comments=[{"path": "a.py", "line": 10, "body": "nit"}],
        )
        url = create_review("owner/repo", 1, "abc1234", review, "review-pr")
        assert url == "https://github.com/owner/repo/pull/1#review-123"
        payload = mock_submit.call_args[0][2]
        assert len(payload["comments"]) == 1

    @patch("run_review._submit_review")
    @patch("run_review.fetch_diff_lines")
    def test_filters_out_of_diff_comments(self, mock_diff, mock_submit, frozen_now):
        mock_diff.return_value = {"a.py": {10}}
        mock_submit.return_value = "https://github.com/review"
        review = ReviewOutput(
            summary="Review",
            event="REQUEST_CHANGES",
            comments=[
                {"path": "a.py", "line": 10, "body": "valid"},
                {"path": "a.py", "line": 99, "body": "not in diff"},
                {"path": "b.py", "line": 5, "body": "file not in diff"},
            ],
        )
        create_review("owner/repo", 1, "abc1234", review, "review-pr")
        payload = mock_submit.call_args[0][2]
        assert len(payload["comments"]) == 1
        assert payload["comments"][0]["line"] == 10

    @patch("run_review._submit_review")
    @patch("run_review.fetch_diff_lines")
    def test_retries_with_comment_event_on_own_pr(self, mock_diff, mock_submit, frozen_now):
        mock_diff.return_value = {"a.py": {10}}
        mock_submit.side_effect = [None, "https://github.com/review"]
        review = ReviewOutput(
            summary="Review",
            event="REQUEST_CHANGES",
            comments=[{"path": "a.py", "line": 10, "body": "nit"}],
        )
        url = create_review("owner/repo", 1, "abc1234", review, "review-pr")
        assert url == "https://github.com/review"
        assert mock_submit.call_count == 2
        retry_payload = mock_submit.call_args_list[1][0][2]
        assert retry_payload["event"] == "COMMENT"
        assert len(retry_payload["comments"]) == 1

    @patch("run_review._submit_review")
    @patch("run_review.fetch_diff_lines")
    def test_retries_without_comments_on_total_failure(self, mock_diff, mock_submit, frozen_now):
        mock_diff.return_value = {"a.py": {10}}
        mock_submit.side_effect = [None, None, "https://github.com/review"]
        review = ReviewOutput(
            summary="Review",
            event="REQUEST_CHANGES",
            comments=[{"path": "a.py", "line": 10, "body": "nit"}],
        )
        url = create_review("owner/repo", 1, "abc1234", review, "review-pr")
        assert url == "https://github.com/review"
        assert mock_submit.call_count == 3
        final_payload = mock_submit.call_args_list[2][0][2]
        assert final_payload["comments"] == []
        assert final_payload["event"] == "COMMENT"

    @patch("run_review._submit_review")
    @patch("run_review.fetch_diff_lines")
    def test_failure_returns_none(self, mock_diff, mock_submit, frozen_now):
        mock_diff.return_value = {}
        mock_submit.return_value = None
        review = ReviewOutput(summary="Review", event="APPROVE", comments=[])
        assert create_review("owner/repo", 1, "abc1234", review, "review-pr") is None


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
    @patch("run_review.minimize_comment", return_value=True)
    @patch("run_review.find_existing_comments")
    def test_minimizes_old_comments(self, mock_find, mock_min, mock_create, frozen_now):
        mock_find.return_value = [{"id": 1, "node_id": "N1"}, {"id": 2, "node_id": "N2"}]
        post_comment("owner/repo", 1, "new review", "review-pr", "abc1234def")
        assert mock_min.call_count == 2
        mock_create.assert_called_once()

    @patch("run_review._create_comment")
    @patch("run_review.find_existing_comments", return_value=[])
    def test_footer_includes_sha(self, mock_find, mock_create, frozen_now):
        post_comment("owner/repo", 1, "body", "review-pr", "abc1234def5678")
        body = mock_create.call_args[0][2]
        assert "`abc1234`" in body
        assert "<!-- claude-review-daemon:review-pr -->" in body

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

    @staticmethod
    def _stream_lines(result_text="Review result", assistant_texts=None, cost_usd=0.01, num_turns=2):
        lines = []
        for text in (assistant_texts or []):
            lines.append(_assistant_event(text))
        lines.append(_result_event(result_text, cost_usd=cost_usd, num_turns=num_turns, session_id="s1"))
        return lines

    @patch("run_review.post_comment")
    @patch("run_review.fetch_existing_review_comments", return_value=[])
    @patch("run_review.subprocess.Popen")
    @patch("run_review.run")
    @patch("run_review.os.path.exists", return_value=False)
    @patch("run_review.os.path.isfile", return_value=True)
    @patch("builtins.open", mock_open(read_data="skill content"))
    def test_happy_path_plain_text_fallback(self, mock_isfile, mock_exists, mock_run_wrap, mock_popen, mock_fetch, mock_post):
        mock_popen.return_value = make_mock_popen(stdout_lines=self._stream_lines("Review result"))
        do_review(**self.COMMON_KWARGS)
        mock_post.assert_called_once()
        assert "Review result" in mock_post.call_args[0][2]

    @patch("run_review.create_review", return_value="https://github.com/review/1")
    @patch("run_review.minimize_comment", return_value=True)
    @patch("run_review.find_existing_comments", return_value=[{"id": 1, "node_id": "N1"}])
    @patch("run_review.fetch_existing_review_comments", return_value=[])
    @patch("run_review.subprocess.Popen")
    @patch("run_review.run")
    @patch("run_review.os.path.exists", return_value=False)
    @patch("run_review.os.path.isfile", return_value=True)
    @patch("builtins.open", mock_open(read_data="skill content"))
    def test_happy_path_json_review(self, mock_isfile, mock_exists, mock_run_wrap, mock_popen,
                                     mock_fetch, mock_find, mock_min, mock_create):
        json_output = '{"summary": "LGTM", "event": "APPROVE", "comments": [{"path": "a.py", "line": 10, "body": "nit"}]}'
        mock_popen.return_value = make_mock_popen(stdout_lines=self._stream_lines(json_output))
        do_review(**self.COMMON_KWARGS)
        mock_create.assert_called_once()
        mock_min.assert_called_once_with("N1")

    @patch("run_review.create_review", return_value="https://github.com/review/1")
    @patch("run_review.minimize_comment", return_value=True)
    @patch("run_review.find_existing_comments", return_value=[])
    @patch("run_review.fetch_existing_review_comments", return_value=[])
    @patch("run_review.subprocess.Popen")
    @patch("run_review.run")
    @patch("run_review.os.path.exists", return_value=False)
    @patch("run_review.os.path.isfile", return_value=True)
    @patch("builtins.open", mock_open(read_data="skill content"))
    def test_markdown_fallback_review(self, mock_isfile, mock_exists, mock_run_wrap, mock_popen,
                                      mock_fetch, mock_find, mock_min, mock_create):
        md_output = (
            "Good code.\n\n"
            "**`src/main.py` (line 42)**\n"
            "This is too long.\n"
        )
        mock_popen.return_value = make_mock_popen(stdout_lines=self._stream_lines(md_output))
        do_review(**self.COMMON_KWARGS)
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
