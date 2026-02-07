#!/usr/bin/env python3
"""Claude CI Bridge - Polling daemon for Claude Max PR reviews.

A standalone daemon that polls GitHub for open PRs, tracks reviewed commits
in a state file, and triggers Claude reviews when PR heads change.
"""

import json
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    import tomllib
except ImportError:
    sys.exit("Python 3.11+ required for tomllib support")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("claude-ci-bridge")

GITHUB_API = "https://api.github.com"
STATE_VERSION = 1


@dataclass
class RepoConfig:
    name: str
    skill: str = "review-pr"
    branches: list[str] = field(default_factory=list)
    enabled: bool = True


@dataclass
class Config:
    interval_seconds: int = 300
    max_concurrent_reviews: int = 3
    state_file: str = "./state.json"
    repo_dir: str = "./repos"
    repos: list[RepoConfig] = field(default_factory=list)


def load_config(path: str) -> Config:
    with open(path, "rb") as f:
        data = tomllib.load(f)

    polling = data.get("polling", {})
    paths = data.get("paths", {})

    repos = []
    for r in data.get("repos", []):
        repos.append(RepoConfig(
            name=r["name"],
            skill=r.get("skill", "review-pr"),
            branches=r.get("branches", []),
            enabled=r.get("enabled", True),
        ))

    return Config(
        interval_seconds=polling.get("interval_seconds", 300),
        max_concurrent_reviews=polling.get("max_concurrent_reviews", 3),
        state_file=paths.get("state_file", "./state.json"),
        repo_dir=paths.get("repo_dir", "./repos"),
        repos=repos,
    )


class StateManager:
    def __init__(self, path: str):
        self.path = Path(path)
        self.data: dict[str, Any] = {"version": STATE_VERSION, "repos": {}}
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                with open(self.path) as f:
                    self.data = json.load(f)
                log.info("Loaded state from %s", self.path)
            except (json.JSONDecodeError, OSError) as e:
                log.warning("Failed to load state file, starting fresh: %s", e)
                self.data = {"version": STATE_VERSION, "repos": {}}

    def save(self):
        tmp_path = self.path.with_suffix(".tmp")
        with open(tmp_path, "w") as f:
            json.dump(self.data, f, indent=2)
        tmp_path.replace(self.path)

    def get_etag(self, repo: str) -> str | None:
        return self.data["repos"].get(repo, {}).get("etag")

    def set_etag(self, repo: str, etag: str):
        if repo not in self.data["repos"]:
            self.data["repos"][repo] = {"prs": {}}
        self.data["repos"][repo]["etag"] = etag

    def get_reviewed_sha(self, repo: str, pr_number: int) -> str | None:
        return self.data["repos"].get(repo, {}).get("prs", {}).get(str(pr_number), {}).get("head_sha")

    def mark_reviewed(self, repo: str, pr_number: int, head_sha: str, status: str = "completed"):
        if repo not in self.data["repos"]:
            self.data["repos"][repo] = {"prs": {}}
        if "prs" not in self.data["repos"][repo]:
            self.data["repos"][repo]["prs"] = {}

        self.data["repos"][repo]["prs"][str(pr_number)] = {
            "head_sha": head_sha,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
            "review_status": status,
        }

    def get_review_status(self, repo: str, pr_number: int) -> str | None:
        return self.data["repos"].get(repo, {}).get("prs", {}).get(str(pr_number), {}).get("review_status")

    def cleanup_closed_prs(self, repo: str, open_pr_numbers: set[int]):
        if repo not in self.data["repos"]:
            return
        prs = self.data["repos"][repo].get("prs", {})
        closed = [pr for pr in prs if int(pr) not in open_pr_numbers]
        for pr in closed:
            del prs[pr]
            log.info("Cleaned up closed PR %s#%s from state", repo, pr)


class GitHubClient:
    def __init__(self):
        self.token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not self.token:
            log.warning("No GitHub token found (GH_TOKEN or GITHUB_TOKEN). API rate limits will be very restrictive.")
        self.rate_limit_reset: float = 0
        self.rate_limit_remaining: int = 5000

    def _request(self, endpoint: str, etag: str | None = None) -> tuple[int, dict | list | None, str | None]:
        if time.time() < self.rate_limit_reset and self.rate_limit_remaining == 0:
            wait = self.rate_limit_reset - time.time()
            log.warning("Rate limited, waiting %.0f seconds", wait)
            time.sleep(wait)

        url = f"{GITHUB_API}{endpoint}"
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if etag:
            headers["If-None-Match"] = etag

        req = Request(url, headers=headers)

        try:
            with urlopen(req, timeout=30) as resp:
                self._update_rate_limits(resp.headers)
                new_etag = resp.headers.get("ETag")
                data = json.loads(resp.read().decode())
                return resp.status, data, new_etag
        except HTTPError as e:
            self._update_rate_limits(e.headers)
            if e.code == 304:
                return 304, None, etag
            if e.code == 403 and self.rate_limit_remaining == 0:
                log.error("Rate limit exceeded")
                return 403, None, None
            if e.code == 404:
                log.warning("Resource not found: %s", endpoint)
                return 404, None, None
            log.error("HTTP error %d for %s: %s", e.code, endpoint, e.reason)
            return e.code, None, None
        except URLError as e:
            log.error("Network error for %s: %s", endpoint, e.reason)
            return 0, None, None
        except TimeoutError:
            log.error("Request timeout for %s", endpoint)
            return 0, None, None

    def _update_rate_limits(self, headers):
        remaining = headers.get("X-RateLimit-Remaining")
        reset = headers.get("X-RateLimit-Reset")
        if remaining is not None:
            self.rate_limit_remaining = int(remaining)
        if reset is not None:
            self.rate_limit_reset = float(reset)

    def get_open_prs(self, repo: str, etag: str | None = None) -> tuple[int, list[dict] | None, str | None]:
        return self._request(f"/repos/{repo}/pulls?state=open&per_page=100", etag)


class ReviewCoordinator:
    def __init__(self, config: Config, state: StateManager, github: GitHubClient):
        self.config = config
        self.state = state
        self.github = github
        self.active_reviews: dict[str, subprocess.Popen] = {}

    def cleanup_finished_reviews(self):
        finished = []
        for key, proc in self.active_reviews.items():
            ret = proc.poll()
            if ret is not None:
                repo, pr = key.rsplit("#", 1)
                if ret == 0:
                    log.info("Review completed: %s", key)
                else:
                    log.warning("Review failed with code %d: %s", ret, key)
                finished.append(key)

        for key in finished:
            del self.active_reviews[key]

    def can_start_review(self) -> bool:
        self.cleanup_finished_reviews()
        return len(self.active_reviews) < self.config.max_concurrent_reviews

    def is_reviewing(self, repo: str, pr_number: int) -> bool:
        return f"{repo}#{pr_number}" in self.active_reviews

    def start_review(self, repo: str, pr: dict, skill: str):
        pr_number = pr["number"]
        head_sha = pr["head"]["sha"]
        branch = pr["head"]["ref"]
        base_branch = pr["base"]["ref"]

        key = f"{repo}#{pr_number}"
        log.info("Starting review for %s (head: %s)", key, head_sha[:8])

        self.state.mark_reviewed(repo, pr_number, head_sha, status="in_progress")
        self.state.save()

        proc = subprocess.Popen(
            [
                sys.executable,
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_review.py"),
                "--repo", repo,
                "--pr-number", str(pr_number),
                "--branch", branch,
                "--base-branch", base_branch,
                "--skill", skill,
                "--repo-dir", self.config.repo_dir,
                "--head-sha", head_sha,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        self.active_reviews[key] = proc


class Daemon:
    def __init__(self, config_path: str):
        self.config = load_config(config_path)
        self.state = StateManager(self.config.state_file)
        self.github = GitHubClient()
        self.coordinator = ReviewCoordinator(self.config, self.state, self.github)
        self.running = True
        self.backoff = 30

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _handle_signal(self, signum, frame):
        log.info("Received signal %d, shutting down gracefully...", signum)
        self.running = False

    def poll_repo(self, repo_config: RepoConfig):
        if not repo_config.enabled:
            return

        repo = repo_config.name
        etag = self.state.get_etag(repo)

        status, prs, new_etag = self.github.get_open_prs(repo, etag)

        if status == 304:
            log.debug("No changes for %s (ETag match)", repo)
            return

        if status == 404:
            log.error("Repository not found: %s", repo)
            return

        if status not in (200, 0) or prs is None:
            log.warning("Failed to fetch PRs for %s (status %d)", repo, status)
            return

        if new_etag:
            self.state.set_etag(repo, new_etag)

        open_pr_numbers = set()

        for pr in prs:
            pr_number = pr["number"]
            head_sha = pr["head"]["sha"]
            base_branch = pr["base"]["ref"]
            open_pr_numbers.add(pr_number)

            if repo_config.branches and base_branch not in repo_config.branches:
                continue

            if self.coordinator.is_reviewing(repo, pr_number):
                continue

            reviewed_sha = self.state.get_reviewed_sha(repo, pr_number)
            review_status = self.state.get_review_status(repo, pr_number)

            needs_review = (
                reviewed_sha is None or
                reviewed_sha != head_sha or
                review_status == "in_progress"
            )

            if needs_review and self.coordinator.can_start_review():
                self.coordinator.start_review(repo, pr, repo_config.skill)

        self.state.cleanup_closed_prs(repo, open_pr_numbers)

    def run(self):
        log.info("Claude CI Bridge daemon starting")
        log.info("Polling interval: %d seconds", self.config.interval_seconds)
        log.info("Max concurrent reviews: %d", self.config.max_concurrent_reviews)
        log.info("Monitoring %d repos", len(self.config.repos))

        for repo in self.config.repos:
            if repo.enabled:
                log.info("  - %s (skill: %s, branches: %s)",
                         repo.name, repo.skill, repo.branches or "all")

        while self.running:
            try:
                for repo_config in self.config.repos:
                    if not self.running:
                        break
                    self.poll_repo(repo_config)

                self.state.save()
                self.backoff = 30

            except Exception:
                log.exception("Error during poll cycle")
                log.info("Backing off for %d seconds", self.backoff)
                time.sleep(self.backoff)
                self.backoff = min(self.backoff * 2, 300)
                continue

            if self.running:
                time.sleep(self.config.interval_seconds)

        log.info("Waiting for active reviews to complete...")
        while self.coordinator.active_reviews:
            self.coordinator.cleanup_finished_reviews()
            if self.coordinator.active_reviews:
                time.sleep(5)

        self.state.save()
        log.info("Daemon stopped")


def main():
    config_path = os.environ.get("CONFIG_FILE", "config.toml")

    if not os.path.exists(config_path):
        log.error("Config file not found: %s", config_path)
        log.error("Copy config.toml.example to config.toml and configure your repos")
        sys.exit(1)

    daemon = Daemon(config_path)
    daemon.run()


if __name__ == "__main__":
    main()
