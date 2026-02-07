import json
from unittest.mock import MagicMock, patch

import pytest

from slack_notify import extract_tldr, get_pr_title, notify_review_posted
from tests.helpers import make_completed_process


# ---------------------------------------------------------------------------
# extract_tldr
# ---------------------------------------------------------------------------

class TestExtractTldr:
    def test_extracts_summary_section(self):
        output = "## Summary\nThis PR fixes the login bug.\n\n## Details\nMore stuff."
        assert extract_tldr(output) == "This PR fixes the login bug."

    def test_extracts_tldr_heading(self):
        output = "## TL;DR\nQuick fix for auth.\n\n## Changes"
        assert extract_tldr(output) == "Quick fix for auth."

    def test_extracts_overview_heading(self):
        output = "## Overview\nRefactored the module."
        assert extract_tldr(output) == "Refactored the module."

    def test_case_insensitive_heading(self):
        output = "## summary\nLowercase heading works."
        assert extract_tldr(output) == "Lowercase heading works."

    def test_fallback_to_first_paragraph(self):
        output = "# Title\nFirst real paragraph here."
        assert extract_tldr(output) == "First real paragraph here."

    def test_skips_html_comments(self):
        output = "<!-- marker -->\nActual content."
        assert extract_tldr(output) == "Actual content."

    def test_strips_markdown_links(self):
        output = "## Summary\nSee [the docs](https://example.com) for details."
        assert extract_tldr(output) == "See the docs for details."

    def test_strips_markdown_formatting(self):
        output = "## Summary\nThis is **bold** and _italic_ and `code`."
        assert extract_tldr(output) == "This is bold and italic and code."

    def test_truncation_on_word_boundary(self):
        output = "## Summary\n" + "word " * 100
        result = extract_tldr(output, max_length=30)
        assert result.endswith("...")
        assert len(result) <= 34  # 30 + word boundary + "..."

    def test_empty_input(self):
        assert extract_tldr("") == ""

    def test_only_headings(self):
        assert extract_tldr("# Heading\n## Another") == ""

    def test_multiline_summary_section(self):
        output = "## Summary\nLine one.\nLine two.\n\n## Next"
        result = extract_tldr(output)
        assert "Line one." in result
        assert "Line two." in result


# ---------------------------------------------------------------------------
# get_pr_title
# ---------------------------------------------------------------------------

class TestGetPrTitle:
    @patch("slack_notify.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = make_completed_process(stdout="Fix login bug\n")
        assert get_pr_title("owner/repo", 42) == "Fix login bug"

    @patch("slack_notify.subprocess.run")
    def test_fallback_on_failure(self, mock_run):
        mock_run.return_value = make_completed_process(returncode=1, stdout="")
        assert get_pr_title("owner/repo", 42) == "PR #42"

    @patch("slack_notify.subprocess.run")
    def test_fallback_on_exception(self, mock_run):
        mock_run.side_effect = OSError("network error")
        assert get_pr_title("owner/repo", 7) == "PR #7"

    @patch("slack_notify.subprocess.run")
    def test_fallback_on_timeout(self, mock_run):
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=30)
        assert get_pr_title("owner/repo", 1) == "PR #1"


# ---------------------------------------------------------------------------
# notify_review_posted
# ---------------------------------------------------------------------------

class TestNotifyReviewPosted:
    @patch("slack_notify.urlopen")
    @patch("slack_notify.get_pr_title", return_value="Fix login")
    @patch.dict("os.environ", {"SLACK_WEBHOOK_URL": "https://hooks.slack.com/test"})
    def test_sends_payload_with_correct_structure(self, mock_title, mock_urlopen):
        notify_review_posted("owner/repo", 42, "## Summary\nFixed it.", "https://github.com/comment/1")

        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data)
        blocks = payload["blocks"]

        assert len(blocks) == 3
        assert ":mag:" in blocks[0]["text"]["text"]
        assert "Fix login" in blocks[0]["text"]["text"]
        assert "owner/repo#42" in blocks[0]["text"]["text"]
        assert "TL;DR:" in blocks[1]["text"]["text"]
        assert blocks[2]["type"] == "actions"
        assert blocks[2]["elements"][0]["url"] == "https://github.com/comment/1"

    @patch("slack_notify.urlopen")
    @patch("slack_notify.get_pr_title", return_value="Title")
    @patch.dict("os.environ", {}, clear=True)
    def test_noop_without_webhook_url(self, mock_title, mock_urlopen):
        notify_review_posted("owner/repo", 1, "output", None)
        mock_urlopen.assert_not_called()
        mock_title.assert_not_called()

    @patch("slack_notify.urlopen")
    @patch("slack_notify.get_pr_title", return_value="Title")
    @patch.dict("os.environ", {"SLACK_WEBHOOK_URL": "https://hooks.slack.com/test"})
    def test_omits_button_when_no_comment_url(self, mock_title, mock_urlopen):
        notify_review_posted("owner/repo", 1, "## Summary\nDone.", None)

        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data)
        blocks = payload["blocks"]

        action_blocks = [b for b in blocks if b["type"] == "actions"]
        assert len(action_blocks) == 0

    @patch("slack_notify.urlopen")
    @patch("slack_notify.get_pr_title", return_value="Title")
    @patch.dict("os.environ", {"SLACK_WEBHOOK_URL": "https://hooks.slack.com/test"})
    def test_network_error_logged_not_raised(self, mock_title, mock_urlopen):
        mock_urlopen.side_effect = OSError("connection refused")
        notify_review_posted("owner/repo", 1, "output", "https://url")

    @patch("slack_notify.urlopen")
    @patch("slack_notify.get_pr_title", return_value="Title")
    @patch.dict("os.environ", {"SLACK_WEBHOOK_URL": "https://hooks.slack.com/test"})
    def test_no_tldr_omits_tldr_block(self, mock_title, mock_urlopen):
        notify_review_posted("owner/repo", 1, "", "https://url")

        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data)
        blocks = payload["blocks"]

        tldr_blocks = [b for b in blocks if b.get("text", {}).get("text", "").startswith("*TL;DR:*")]
        assert len(tldr_blocks) == 0
