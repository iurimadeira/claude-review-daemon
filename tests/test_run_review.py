import subprocess
from unittest.mock import MagicMock, call, mock_open, patch

import pytest

import run_review
from run_review import (
    MAX_COMMENT_LENGTH,
    _create_comment,
    find_existing_comment,
    main,
    run,
    run_review as do_review,
    truncate_output,
    upsert_comment,
)

from tests.helpers import FROZEN_NOW, make_completed_process


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
# find_existing_comment
# ---------------------------------------------------------------------------

class TestFindExistingComment:
    @patch("run_review.subprocess.run")
    def test_comment_found(self, mock_run):
        mock_run.return_value = make_completed_process(stdout="12345\n")
        assert find_existing_comment("owner/repo", 1, "review-pr") == 12345

    @patch("run_review.subprocess.run")
    def test_empty_stdout_returns_none(self, mock_run):
        mock_run.return_value = make_completed_process(stdout="")
        assert find_existing_comment("owner/repo", 1, "review-pr") is None

    @patch("run_review.subprocess.run")
    def test_nonzero_returncode_returns_none(self, mock_run):
        mock_run.return_value = make_completed_process(returncode=1, stdout="12345")
        assert find_existing_comment("owner/repo", 1, "review-pr") is None

    @patch("run_review.subprocess.run")
    def test_exception_returns_none(self, mock_run):
        mock_run.side_effect = OSError("boom")
        assert find_existing_comment("owner/repo", 1, "review-pr") is None

    @patch("run_review.subprocess.run")
    def test_command_includes_marker_in_jq(self, mock_run):
        mock_run.return_value = make_completed_process(stdout="")
        find_existing_comment("owner/repo", 7, "custom-skill")
        args = mock_run.call_args[0][0]
        assert "gh" in args
        assert "/repos/owner/repo/issues/7/comments" in args
        jq_arg = [a for a in args if "select(" in a][0]
        assert "<!-- claude-review-daemon:custom-skill -->" in jq_arg


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
# upsert_comment
# ---------------------------------------------------------------------------

class TestUpsertComment:
    @patch("run_review._create_comment")
    @patch("run_review.find_existing_comment", return_value=None)
    def test_no_existing_creates_new(self, mock_find, mock_create, frozen_now):
        upsert_comment("owner/repo", 1, "review output", "review-pr", "abc1234def")
        mock_create.assert_called_once()
        body = mock_create.call_args[0][2]
        assert "<!-- claude-review-daemon:review-pr -->" in body
        assert "review output" in body

    @patch("run_review.subprocess.run")
    @patch("run_review.find_existing_comment", return_value=999)
    def test_existing_updates_via_patch(self, mock_find, mock_run, frozen_now):
        mock_run.return_value = make_completed_process()
        upsert_comment("owner/repo", 1, "updated", "review-pr")
        args = mock_run.call_args[0][0]
        assert "PATCH" in args
        assert "/repos/owner/repo/issues/comments/999" in args

    @patch("run_review._create_comment")
    @patch("run_review.subprocess.run")
    @patch("run_review.find_existing_comment", return_value=999)
    def test_patch_failure_falls_back_to_create(self, mock_find, mock_run, mock_create, frozen_now):
        mock_run.return_value = make_completed_process(returncode=1, stderr="fail")
        upsert_comment("owner/repo", 1, "body", "review-pr")
        mock_create.assert_called_once()

    @patch("run_review._create_comment")
    @patch("run_review.find_existing_comment", return_value=None)
    def test_footer_includes_sha_when_provided(self, mock_find, mock_create, frozen_now):
        upsert_comment("owner/repo", 1, "body", "review-pr", "abc1234def5678")
        body = mock_create.call_args[0][2]
        assert "`abc1234`" in body

    @patch("run_review._create_comment")
    @patch("run_review.find_existing_comment", return_value=None)
    def test_footer_timestamp_only_when_no_sha(self, mock_find, mock_create, frozen_now):
        upsert_comment("owner/repo", 1, "body", "review-pr", None)
        body = mock_create.call_args[0][2]
        assert "Reviewed commit" not in body
        assert "2025-06-15 12:00 UTC" in body

    @patch("run_review._create_comment")
    @patch("run_review.find_existing_comment", return_value=None)
    def test_long_body_truncated(self, mock_find, mock_create, frozen_now):
        upsert_comment("owner/repo", 1, "x" * (MAX_COMMENT_LENGTH + 500), "review-pr")
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

    @patch("run_review.upsert_comment")
    @patch("run_review.subprocess.run")
    @patch("run_review.run")
    @patch("run_review.os.path.exists", return_value=False)
    @patch("run_review.os.path.isfile", return_value=True)
    @patch("builtins.open", mock_open(read_data="skill content"))
    def test_happy_path(self, mock_isfile, mock_exists, mock_run_wrap, mock_subproc, mock_upsert):
        mock_subproc.return_value = make_completed_process(stdout="Review result")
        do_review(**self.COMMON_KWARGS)
        mock_upsert.assert_called_once()
        assert "Review result" in mock_upsert.call_args[0][2]

    @patch("run_review.upsert_comment")
    @patch("run_review.subprocess.run")
    @patch("run_review.run")
    @patch("run_review.os.path.exists", return_value=True)
    @patch("run_review.os.path.isfile", return_value=True)
    @patch("builtins.open", mock_open(read_data="skill"))
    def test_stale_worktree_removed(self, mock_isfile, mock_exists, mock_run_wrap, mock_subproc, mock_upsert):
        mock_subproc.return_value = make_completed_process(stdout="ok")
        do_review(**self.COMMON_KWARGS)
        remove_calls = [c for c in mock_run_wrap.call_args_list if "worktree" in str(c) and "remove" in str(c)]
        assert len(remove_calls) >= 1

    @patch("run_review.upsert_comment")
    @patch("run_review.run")
    @patch("run_review.os.path.exists", return_value=False)
    @patch("run_review.os.path.isfile", return_value=False)
    def test_skill_not_found(self, mock_isfile, mock_exists, mock_run_wrap, mock_upsert):
        do_review(**self.COMMON_KWARGS)
        body = mock_upsert.call_args[0][2]
        assert "Skill file not found" in body

    @patch("run_review.upsert_comment")
    @patch("run_review.subprocess.run")
    @patch("run_review.run")
    @patch("run_review.os.path.exists", return_value=False)
    @patch("run_review.os.path.isfile", return_value=True)
    @patch("builtins.open", mock_open(read_data="skill"))
    def test_claude_nonzero_exit(self, mock_isfile, mock_exists, mock_run_wrap, mock_subproc, mock_upsert):
        mock_subproc.return_value = make_completed_process(returncode=1, stdout="partial", stderr="error detail")
        do_review(**self.COMMON_KWARGS)
        body = mock_upsert.call_args[0][2]
        assert "partial" in body or "error detail" in body

    @patch("run_review.upsert_comment")
    @patch("run_review.subprocess.run")
    @patch("run_review.run")
    @patch("run_review.os.path.exists", return_value=False)
    @patch("run_review.os.path.isfile", return_value=True)
    @patch("builtins.open", mock_open(read_data="skill"))
    def test_claude_empty_output(self, mock_isfile, mock_exists, mock_run_wrap, mock_subproc, mock_upsert):
        mock_subproc.return_value = make_completed_process(stdout="   \n  ")
        do_review(**self.COMMON_KWARGS)
        body = mock_upsert.call_args[0][2]
        assert "produced no output" in body

    @patch("run_review.upsert_comment")
    @patch("run_review.subprocess.run")
    @patch("run_review.run")
    @patch("run_review.os.path.exists", return_value=False)
    @patch("run_review.os.path.isfile", return_value=True)
    @patch("builtins.open", mock_open(read_data="skill"))
    def test_timeout(self, mock_isfile, mock_exists, mock_run_wrap, mock_subproc, mock_upsert):
        mock_subproc.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=3600)
        do_review(**self.COMMON_KWARGS)
        body = mock_upsert.call_args[0][2]
        assert "timed out" in body

    @patch("run_review.upsert_comment")
    @patch("run_review.subprocess.run")
    @patch("run_review.run")
    @patch("run_review.os.path.exists", return_value=False)
    @patch("run_review.os.path.isfile", return_value=True)
    @patch("builtins.open", mock_open(read_data="skill"))
    def test_generic_exception(self, mock_isfile, mock_exists, mock_run_wrap, mock_subproc, mock_upsert):
        mock_subproc.side_effect = RuntimeError("unexpected")
        do_review(**self.COMMON_KWARGS)
        body = mock_upsert.call_args[0][2]
        assert "RuntimeError" in body

    @patch("run_review.upsert_comment")
    @patch("run_review.run")
    @patch("run_review.os.path.exists")
    @patch("run_review.os.path.isfile", return_value=False)
    def test_finally_always_cleans_up(self, mock_isfile, mock_exists_fn, mock_run_wrap, mock_upsert):
        mock_exists_fn.side_effect = [False, True]
        do_review(**self.COMMON_KWARGS)
        cleanup_calls = [c for c in mock_run_wrap.call_args_list if "worktree" in str(c) and "remove" in str(c)]
        assert len(cleanup_calls) >= 1

    @patch("run_review.upsert_comment")
    @patch("run_review.run")
    @patch("run_review.os.path.exists")
    @patch("run_review.os.path.isfile", return_value=False)
    def test_cleanup_failure_swallowed(self, mock_isfile, mock_exists_fn, mock_run_wrap, mock_upsert):
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
