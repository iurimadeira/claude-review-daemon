import subprocess
from datetime import datetime, timezone
from unittest.mock import MagicMock

from bridge import Config, RepoConfig

FROZEN_NOW = datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


def make_completed_process(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr,
    )


def sample_pr_payload(number=42, head_sha="abc1234def5678", branch="feature-x", base="main"):
    return {
        "number": number,
        "head": {"sha": head_sha, "ref": branch},
        "base": {"ref": base},
    }


def sample_repo_config(name="owner/repo", skill="review-pr", branches=None, enabled=True):
    return RepoConfig(name=name, skill=skill, branches=branches or [], enabled=enabled)


def make_mock_popen(stdout_lines=None, returncode=0, stderr="", wait_side_effect=None):
    mock_proc = MagicMock()
    mock_proc.stdout = iter(stdout_lines or [])
    mock_proc.stderr.read.return_value = stderr
    mock_proc.returncode = returncode
    mock_proc.poll.return_value = returncode
    if wait_side_effect:
        mock_proc.wait.side_effect = wait_side_effect
    else:
        mock_proc.wait.return_value = None
    return mock_proc


def sample_config(**overrides):
    defaults = dict(
        interval_seconds=60,
        max_concurrent_reviews=3,
        state_file="./state.json",
        repo_dir="./repos",
        repos=[sample_repo_config()],
    )
    defaults.update(overrides)
    return Config(**defaults)
