import subprocess
from datetime import datetime, timezone

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


def sample_config(**overrides):
    defaults = dict(
        interval_seconds=300,
        max_concurrent_reviews=3,
        state_file="./state.json",
        repo_dir="./repos",
        repos=[sample_repo_config()],
    )
    defaults.update(overrides)
    return Config(**defaults)
