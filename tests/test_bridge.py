import json
import subprocess
import time
from io import BytesIO
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

import bridge
from bridge import (
    Config,
    Daemon,
    GitHubClient,
    RepoConfig,
    ReviewCoordinator,
    StateManager,
    load_config,
)

from tests.helpers import (
    FROZEN_NOW,
    make_completed_process,
    sample_config,
    sample_pr_payload,
    sample_repo_config,
)


# ---------------------------------------------------------------------------
# Dataclass defaults
# ---------------------------------------------------------------------------

class TestRepoConfig:
    def test_defaults(self):
        rc = RepoConfig(name="o/r")
        assert rc.skill == "review-pr"
        assert rc.branches == []
        assert rc.enabled is True


class TestConfig:
    def test_defaults(self):
        c = Config()
        assert c.interval_seconds == 300
        assert c.max_concurrent_reviews == 3
        assert c.state_file == "./state.json"
        assert c.repo_dir == "./repos"
        assert c.repos == []


# ---------------------------------------------------------------------------
# load_config  (real file I/O via tmp_path)
# ---------------------------------------------------------------------------

class TestLoadConfig:
    def test_full_config(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text("""\
[polling]
interval_seconds = 60
max_concurrent_reviews = 5

[paths]
state_file = "/tmp/state.json"
repo_dir = "/tmp/repos"

[[repos]]
name = "owner/repo"
skill = "audit"
branches = ["main", "develop"]
enabled = true

[[repos]]
name = "owner/other"
""")
        c = load_config(str(cfg))
        assert c.interval_seconds == 60
        assert c.max_concurrent_reviews == 5
        assert c.state_file == "/tmp/state.json"
        assert c.repo_dir == "/tmp/repos"
        assert len(c.repos) == 2
        assert c.repos[0].name == "owner/repo"
        assert c.repos[0].skill == "audit"
        assert c.repos[0].branches == ["main", "develop"]
        assert c.repos[1].skill == "review-pr"
        assert c.repos[1].branches == []

    def test_minimal_config(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text('[[repos]]\nname = "o/r"\n')
        c = load_config(str(cfg))
        assert c.interval_seconds == 300
        assert c.repos[0].name == "o/r"
        assert c.repos[0].skill == "review-pr"

    def test_missing_sections_use_defaults(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text('[[repos]]\nname = "a/b"\n')
        c = load_config(str(cfg))
        assert c.interval_seconds == 300
        assert c.repo_dir == "./repos"

    def test_enabled_false(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text('[[repos]]\nname = "a/b"\nenabled = false\n')
        c = load_config(str(cfg))
        assert c.repos[0].enabled is False


# ---------------------------------------------------------------------------
# StateManager  (real file I/O via tmp_path)
# ---------------------------------------------------------------------------

class TestStateManager:
    def test_fresh_state(self, tmp_path):
        sm = StateManager(str(tmp_path / "state.json"))
        assert sm.data["version"] == 1
        assert sm.data["repos"] == {}

    def test_load_existing(self, tmp_path):
        p = tmp_path / "state.json"
        p.write_text(json.dumps({"version": 1, "repos": {"o/r": {"etag": "abc", "prs": {}}}}))
        sm = StateManager(str(p))
        assert sm.get_etag("o/r") == "abc"

    def test_corrupt_json_resets(self, tmp_path):
        p = tmp_path / "state.json"
        p.write_text("{bad json")
        sm = StateManager(str(p))
        assert sm.data["repos"] == {}

    def test_save_reload_roundtrip(self, tmp_path):
        p = tmp_path / "state.json"
        sm = StateManager(str(p))
        sm.set_etag("o/r", "tag1")
        sm.save()
        sm2 = StateManager(str(p))
        assert sm2.get_etag("o/r") == "tag1"

    def test_get_set_etag(self, tmp_path):
        sm = StateManager(str(tmp_path / "s.json"))
        assert sm.get_etag("o/r") is None
        sm.set_etag("o/r", "e1")
        assert sm.get_etag("o/r") == "e1"

    def test_reviewed_sha_and_status(self, tmp_path):
        sm = StateManager(str(tmp_path / "s.json"))
        assert sm.get_reviewed_sha("o/r", 1) is None
        assert sm.get_review_status("o/r", 1) is None
        sm.mark_reviewed("o/r", 1, "sha1", "completed")
        assert sm.get_reviewed_sha("o/r", 1) == "sha1"
        assert sm.get_review_status("o/r", 1) == "completed"

    def test_cleanup_closed_prs(self, tmp_path):
        sm = StateManager(str(tmp_path / "s.json"))
        sm.mark_reviewed("o/r", 1, "sha1")
        sm.mark_reviewed("o/r", 2, "sha2")
        sm.mark_reviewed("o/r", 3, "sha3")
        sm.cleanup_closed_prs("o/r", {1, 3})
        assert sm.get_reviewed_sha("o/r", 1) == "sha1"
        assert sm.get_reviewed_sha("o/r", 2) is None
        assert sm.get_reviewed_sha("o/r", 3) == "sha3"


# ---------------------------------------------------------------------------
# GitHubClient.__init__
# ---------------------------------------------------------------------------

class TestGitHubClientInit:
    @patch.dict("os.environ", {"GH_TOKEN": "gh_tok"}, clear=True)
    def test_token_from_gh_token(self):
        c = GitHubClient()
        assert c.token == "gh_tok"

    @patch.dict("os.environ", {"GITHUB_TOKEN": "ghub_tok"}, clear=True)
    def test_token_from_github_token(self):
        c = GitHubClient()
        assert c.token == "ghub_tok"

    @patch.dict("os.environ", {"GH_TOKEN": "first", "GITHUB_TOKEN": "second"}, clear=True)
    def test_gh_token_takes_precedence(self):
        c = GitHubClient()
        assert c.token == "first"

    @patch.dict("os.environ", {}, clear=True)
    def test_no_token_warns(self):
        c = GitHubClient()
        assert c.token is None


# ---------------------------------------------------------------------------
# GitHubClient._request
# ---------------------------------------------------------------------------

class TestGitHubClientRequest:
    def _make_client(self):
        with patch.dict("os.environ", {"GH_TOKEN": "tok"}, clear=True):
            return GitHubClient()

    def test_200_success(self, mock_urlopen):
        urlopen_mock, resp_mock = mock_urlopen
        resp_mock.status = 200
        resp_mock.read.return_value = json.dumps({"ok": True}).encode()
        resp_mock.headers = {"ETag": '"etag1"'}
        client = self._make_client()
        status, data, etag = client._request("/test")
        assert status == 200
        assert data == {"ok": True}
        assert etag == '"etag1"'

    def test_etag_sent_in_header(self, mock_urlopen):
        urlopen_mock, resp_mock = mock_urlopen
        resp_mock.status = 200
        resp_mock.read.return_value = b"[]"
        resp_mock.headers = {}
        client = self._make_client()
        client._request("/test", etag='"old"')
        req = urlopen_mock.call_args[0][0]
        assert req.get_header("If-none-match") == '"old"'

    def test_304_not_modified(self, mock_urlopen):
        urlopen_mock, _ = mock_urlopen
        err = HTTPError("url", 304, "Not Modified", {}, BytesIO(b""))
        urlopen_mock.side_effect = err
        client = self._make_client()
        status, data, etag = client._request("/test", etag='"old"')
        assert status == 304
        assert data is None
        assert etag == '"old"'

    def test_403_rate_limited(self, mock_urlopen):
        urlopen_mock, _ = mock_urlopen
        headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "9999999999"}
        err = HTTPError("url", 403, "Forbidden", headers, BytesIO(b""))
        urlopen_mock.side_effect = err
        client = self._make_client()
        status, data, etag = client._request("/test")
        assert status == 403

    def test_404(self, mock_urlopen):
        urlopen_mock, _ = mock_urlopen
        err = HTTPError("url", 404, "Not Found", {}, BytesIO(b""))
        urlopen_mock.side_effect = err
        client = self._make_client()
        status, _, _ = client._request("/test")
        assert status == 404

    def test_url_error(self, mock_urlopen):
        urlopen_mock, _ = mock_urlopen
        urlopen_mock.side_effect = URLError("connection refused")
        client = self._make_client()
        status, _, _ = client._request("/test")
        assert status == 0

    def test_timeout_error(self, mock_urlopen):
        urlopen_mock, _ = mock_urlopen
        urlopen_mock.side_effect = TimeoutError()
        client = self._make_client()
        status, _, _ = client._request("/test")
        assert status == 0

    @patch("bridge.time.sleep")
    @patch("bridge.time.time")
    def test_rate_limit_waits(self, mock_time, mock_sleep, mock_urlopen):
        urlopen_mock, resp_mock = mock_urlopen
        resp_mock.status = 200
        resp_mock.read.return_value = b"[]"
        resp_mock.headers = {}
        client = self._make_client()
        client.rate_limit_remaining = 0
        client.rate_limit_reset = 2000.0
        mock_time.return_value = 1000.0
        client._request("/test")
        mock_sleep.assert_called_once_with(1000.0)

    def test_rate_limit_headers_update_state(self, mock_urlopen):
        _, resp_mock = mock_urlopen
        resp_mock.status = 200
        resp_mock.read.return_value = b"[]"
        resp_mock.headers = {"X-RateLimit-Remaining": "42", "X-RateLimit-Reset": "1234567890"}
        client = self._make_client()
        client._request("/test")
        assert client.rate_limit_remaining == 42
        assert client.rate_limit_reset == 1234567890.0


# ---------------------------------------------------------------------------
# ReviewCoordinator
# ---------------------------------------------------------------------------

class TestReviewCoordinator:
    def _make_coordinator(self, max_concurrent=2):
        config = sample_config(max_concurrent_reviews=max_concurrent)
        state = MagicMock()
        github = MagicMock()
        return ReviewCoordinator(config, state, github)

    def test_cleanup_finished_reviews(self):
        coord = self._make_coordinator()
        done_proc = MagicMock()
        done_proc.poll.return_value = 0
        running_proc = MagicMock()
        running_proc.poll.return_value = None
        coord.active_reviews = {"o/r#1": done_proc, "o/r#2": running_proc}
        coord.cleanup_finished_reviews()
        assert "o/r#1" not in coord.active_reviews
        assert "o/r#2" in coord.active_reviews

    def test_can_start_review_under_limit(self):
        coord = self._make_coordinator(max_concurrent=2)
        assert coord.can_start_review() is True

    def test_can_start_review_at_limit(self):
        coord = self._make_coordinator(max_concurrent=1)
        proc = MagicMock()
        proc.poll.return_value = None
        coord.active_reviews["o/r#1"] = proc
        assert coord.can_start_review() is False

    def test_is_reviewing(self):
        coord = self._make_coordinator()
        coord.active_reviews["o/r#5"] = MagicMock()
        assert coord.is_reviewing("o/r", 5) is True
        assert coord.is_reviewing("o/r", 6) is False

    @patch("bridge.subprocess.Popen")
    def test_start_review(self, mock_popen):
        coord = self._make_coordinator()
        pr = sample_pr_payload(number=10, head_sha="deadbeefcafe")
        coord.start_review("o/r", pr, "review-pr")
        assert "o/r#10" in coord.active_reviews
        coord.state.mark_reviewed.assert_called_once_with("o/r", 10, "deadbeefcafe", status="in_progress")
        coord.state.save.assert_called_once()
        args = mock_popen.call_args[0][0]
        assert "--repo" in args
        assert "o/r" in args
        assert "--head-sha" in args
        assert "deadbeefcafe" in args


# ---------------------------------------------------------------------------
# Daemon.poll_repo
# ---------------------------------------------------------------------------

class TestDaemonPollRepo:
    def _make_daemon(self, repo_configs=None):
        cfg_path = "/dev/null"
        with patch("bridge.load_config") as mock_lc, \
             patch("bridge.StateManager") as mock_sm, \
             patch("bridge.GitHubClient") as mock_gh, \
             patch("bridge.ReviewCoordinator") as mock_rc, \
             patch("bridge.signal.signal"):
            mock_lc.return_value = sample_config(
                repos=repo_configs or [sample_repo_config()],
            )
            daemon = Daemon(cfg_path)
        return daemon

    def test_disabled_repo_skipped(self):
        daemon = self._make_daemon()
        rc = sample_repo_config(enabled=False)
        daemon.poll_repo(rc)
        daemon.github.get_open_prs.assert_not_called()

    def test_304_no_reviews(self):
        daemon = self._make_daemon()
        daemon.github.get_open_prs.return_value = (304, None, None)
        daemon.poll_repo(sample_repo_config())
        daemon.coordinator.start_review.assert_not_called()

    def test_404_no_reviews(self):
        daemon = self._make_daemon()
        daemon.github.get_open_prs.return_value = (404, None, None)
        daemon.poll_repo(sample_repo_config())
        daemon.coordinator.start_review.assert_not_called()

    def test_500_no_reviews(self):
        daemon = self._make_daemon()
        daemon.github.get_open_prs.return_value = (500, None, None)
        daemon.poll_repo(sample_repo_config())
        daemon.coordinator.start_review.assert_not_called()

    def test_etag_updated_on_200(self):
        daemon = self._make_daemon()
        daemon.github.get_open_prs.return_value = (200, [], '"new_etag"')
        daemon.poll_repo(sample_repo_config())
        daemon.state.set_etag.assert_called_with("owner/repo", '"new_etag"')

    def test_pr_already_reviewing_skipped(self):
        daemon = self._make_daemon()
        pr = sample_pr_payload(number=1)
        daemon.github.get_open_prs.return_value = (200, [pr], None)
        daemon.coordinator.is_reviewing.return_value = True
        daemon.poll_repo(sample_repo_config())
        daemon.coordinator.start_review.assert_not_called()

    def test_branch_filter_explicit(self):
        daemon = self._make_daemon()
        pr = sample_pr_payload(number=1, base="develop")
        daemon.github.get_open_prs.return_value = (200, [pr], None)
        rc = sample_repo_config(branches=["main"])
        daemon.coordinator.is_reviewing.return_value = False
        daemon.poll_repo(rc)
        daemon.coordinator.start_review.assert_not_called()

    def test_branch_filter_empty_allows_all(self):
        daemon = self._make_daemon()
        pr = sample_pr_payload(number=1, base="develop")
        daemon.github.get_open_prs.return_value = (200, [pr], None)
        daemon.state.get_reviewed_sha.return_value = None
        daemon.coordinator.is_reviewing.return_value = False
        daemon.coordinator.can_start_review.return_value = True
        rc = sample_repo_config(branches=[])
        daemon.poll_repo(rc)
        daemon.coordinator.start_review.assert_called_once()

    def test_same_sha_completed_skipped(self):
        daemon = self._make_daemon()
        pr = sample_pr_payload(number=1, head_sha="abc123")
        daemon.github.get_open_prs.return_value = (200, [pr], None)
        daemon.state.get_reviewed_sha.return_value = "abc123"
        daemon.state.get_review_status.return_value = "completed"
        daemon.coordinator.is_reviewing.return_value = False
        daemon.poll_repo(sample_repo_config())
        daemon.coordinator.start_review.assert_not_called()

    def test_new_sha_triggers_review(self):
        daemon = self._make_daemon()
        pr = sample_pr_payload(number=1, head_sha="new_sha")
        daemon.github.get_open_prs.return_value = (200, [pr], None)
        daemon.state.get_reviewed_sha.return_value = "old_sha"
        daemon.state.get_review_status.return_value = "completed"
        daemon.coordinator.is_reviewing.return_value = False
        daemon.coordinator.can_start_review.return_value = True
        daemon.poll_repo(sample_repo_config())
        daemon.coordinator.start_review.assert_called_once()

    def test_in_progress_triggers_review(self):
        daemon = self._make_daemon()
        pr = sample_pr_payload(number=1, head_sha="same_sha")
        daemon.github.get_open_prs.return_value = (200, [pr], None)
        daemon.state.get_reviewed_sha.return_value = "same_sha"
        daemon.state.get_review_status.return_value = "in_progress"
        daemon.coordinator.is_reviewing.return_value = False
        daemon.coordinator.can_start_review.return_value = True
        daemon.poll_repo(sample_repo_config())
        daemon.coordinator.start_review.assert_called_once()

    def test_at_capacity_skipped(self):
        daemon = self._make_daemon()
        pr = sample_pr_payload(number=1)
        daemon.github.get_open_prs.return_value = (200, [pr], None)
        daemon.state.get_reviewed_sha.return_value = None
        daemon.coordinator.is_reviewing.return_value = False
        daemon.coordinator.can_start_review.return_value = False
        daemon.poll_repo(sample_repo_config())
        daemon.coordinator.start_review.assert_not_called()

    def test_cleanup_closed_prs_called(self):
        daemon = self._make_daemon()
        pr = sample_pr_payload(number=7)
        daemon.github.get_open_prs.return_value = (200, [pr], None)
        daemon.state.get_reviewed_sha.return_value = "sha"
        daemon.state.get_review_status.return_value = "completed"
        daemon.coordinator.is_reviewing.return_value = False
        daemon.poll_repo(sample_repo_config())
        daemon.state.cleanup_closed_prs.assert_called_once_with("owner/repo", {7})


# ---------------------------------------------------------------------------
# Daemon.run
# ---------------------------------------------------------------------------

class TestDaemonRun:
    def _make_daemon(self):
        with patch("bridge.load_config") as mock_lc, \
             patch("bridge.StateManager"), \
             patch("bridge.GitHubClient"), \
             patch("bridge.signal.signal"):
            mock_lc.return_value = sample_config()
            daemon = Daemon("/dev/null")
        return daemon

    @patch("bridge.time.sleep")
    def test_single_poll_cycle(self, mock_sleep):
        daemon = self._make_daemon()
        call_count = 0

        def stop_after_one(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                daemon.running = False

        daemon.poll_repo = MagicMock(side_effect=stop_after_one)
        daemon.coordinator.active_reviews = {}
        daemon.run()
        daemon.poll_repo.assert_called_once()
        daemon.state.save.assert_called()

    @patch("bridge.time.sleep")
    def test_exception_causes_backoff(self, mock_sleep):
        daemon = self._make_daemon()
        call_count = 0

        def fail_then_stop(*args):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("poll error")
            daemon.running = False

        daemon.poll_repo = MagicMock(side_effect=fail_then_stop)
        daemon.coordinator.active_reviews = {}
        daemon.run()
        sleep_calls = [c[0][0] for c in mock_sleep.call_args_list]
        assert 30 in sleep_calls

    @patch("bridge.time.sleep")
    def test_backoff_doubles(self, mock_sleep):
        daemon = self._make_daemon()
        call_count = 0

        def fail_twice_then_stop(*args):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise RuntimeError("fail")
            daemon.running = False

        daemon.poll_repo = MagicMock(side_effect=fail_twice_then_stop)
        daemon.coordinator.active_reviews = {}
        daemon.run()
        sleep_calls = [c[0][0] for c in mock_sleep.call_args_list]
        assert 30 in sleep_calls
        assert 60 in sleep_calls

    @patch("bridge.time.sleep")
    def test_backoff_capped_at_300(self, mock_sleep):
        daemon = self._make_daemon()
        daemon.backoff = 200
        sleep_count = 0

        def stop_after_two_sleeps(seconds):
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count >= 2:
                daemon.running = False

        daemon.poll_repo = MagicMock(side_effect=RuntimeError("fail"))
        mock_sleep.side_effect = stop_after_two_sleeps
        daemon.coordinator.active_reviews = {}
        daemon.run()
        # After 1st fail: backoff = min(200*2, 300) = 300
        # After 2nd fail: backoff = min(300*2, 300) = 300
        assert daemon.backoff == 300

    @patch("bridge.time.sleep")
    def test_backoff_resets_on_success(self, mock_sleep):
        daemon = self._make_daemon()
        daemon.backoff = 120
        call_count = 0

        def succeed_then_stop(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                daemon.running = False

        daemon.poll_repo = MagicMock(side_effect=succeed_then_stop)
        daemon.coordinator.active_reviews = {}
        daemon.run()
        assert daemon.backoff == 30
