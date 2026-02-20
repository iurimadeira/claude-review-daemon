#!/usr/bin/env python3
"""Review execution logic for Claude Review Daemon.

Manages git worktrees, runs Claude with skill injection, and posts
results back to GitHub as PR comments.
"""

import argparse
import json
import logging
import os
import signal as _signal
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from datetime import datetime, timezone

from slack_notify import notify_review_posted

_killed = False


def _handle_term(signum, frame):
    global _killed
    _killed = True
    raise SystemExit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("run-review")


@dataclass
class ClaudeResult:
    result_text: str
    all_assistant_text: list[str]
    cost_usd: float | None
    num_turns: int | None
    is_error: bool
    session_id: str | None


def parse_stream_json(stream) -> ClaudeResult:
    all_assistant_text = []
    result_text = ""
    cost_usd = None
    num_turns = None
    is_error = False
    session_id = None

    for line in stream:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            log.warning("Skipping malformed stream-json line: %s", line[:200])
            continue

        event_type = event.get("type")
        if event_type == "assistant":
            message = event.get("message", {})
            for block in message.get("content", []):
                if block.get("type") == "text":
                    text = block.get("text", "").strip()
                    if text:
                        all_assistant_text.append(text)
        elif event_type == "result":
            result_text = event.get("result", "")
            cost_usd = event.get("cost_usd")
            num_turns = event.get("num_turns")
            is_error = event.get("subtype") == "error"
            session_id = event.get("session_id")

    return ClaudeResult(
        result_text=result_text,
        all_assistant_text=all_assistant_text,
        cost_usd=cost_usd,
        num_turns=num_turns,
        is_error=is_error,
        session_id=session_id,
    )


_STUB_PATTERNS = [
    "review complete",
    "all findings",
    "findings are in",
    "consolidated report above",
    "report above",
    "posted above",
    "summary above",
]
_STUB_MAX_LENGTH = 500


def select_review_output(result: ClaudeResult) -> str:
    text = result.result_text.strip()

    if not text and result.all_assistant_text:
        return "\n\n".join(result.all_assistant_text)

    if not text:
        return ""

    is_stub = (
        len(text) < _STUB_MAX_LENGTH
        and any(p in text.lower() for p in _STUB_PATTERNS)
    )

    if is_stub and result.all_assistant_text:
        log.info(
            "Result looks like a summary stub (%d chars), using full assistant output (%d messages)",
            len(text), len(result.all_assistant_text),
        )
        return "\n\n".join(result.all_assistant_text)

    return text

MAX_COMMENT_LENGTH = 65000  # GitHub comment limit is 65536
COMMENT_MARKER_TEMPLATE = "<!-- claude-review-daemon:{skill} -->"


def run(cmd: list[str], cwd: str | None = None, capture: bool = False) -> subprocess.CompletedProcess:
    log.info("Running: %s", " ".join(cmd))
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=capture,
        text=True,
        timeout=1800,  # 30 min max per command
    )


def run_review(
    repo: str,
    pr_number: int,
    branch: str,
    base_branch: str,
    skill: str,
    repo_dir: str,
    head_sha: str | None = None,
):
    global _killed
    _killed = False
    _signal.signal(_signal.SIGTERM, _handle_term)

    repo_path = os.path.abspath(os.path.join(repo_dir, repo.replace("/", "_")))
    worktree_name = f"pr-{pr_number}"
    worktree_path = os.path.join(repo_path, "worktrees", worktree_name)

    log.info(
        "Starting review: repo=%s pr=#%d branch=%s base=%s skill=%s",
        repo, pr_number, branch, base_branch, skill,
    )

    try:
        # 1. Fetch latest changes
        run(["git", "pull", "--all"], cwd=repo_path)

        # 2. Clean up stale worktree if it exists
        if os.path.exists(worktree_path):
            log.warning("Stale worktree found at %s, removing", worktree_path)
            run(["git", "worktree", "remove", worktree_path, "--force"], cwd=repo_path)

        # 3. Create worktree for this PR
        run(
            ["git", "worktree", "add", worktree_path, f"origin/{branch}"],
            cwd=repo_path,
        )

        # 4. Read the skill file
        skill_path = os.path.join(worktree_path, ".claude", "skills", skill, "SKILL.md")
        if not os.path.isfile(skill_path):
            skill_path = os.path.join(worktree_path, ".claude", "commands", f"{skill}.md")
        if not os.path.isfile(skill_path):
            error_msg = (
                f"Skill file not found. Tried:\n"
                f"- `.claude/skills/{skill}/SKILL.md`\n"
                f"- `.claude/commands/{skill}.md`"
            )
            log.error(error_msg)
            upsert_comment(repo, pr_number, f"**Claude Review Daemon Error**\n\n{error_msg}", skill, head_sha)
            return

        with open(skill_path) as f:
            skill_content = f.read()

        log.info("Loaded skill file: %s (%d bytes)", skill_path, len(skill_content))

        # 5. Run Claude with skill injection
        prompt = (
            f"Execute the following skill for PR #{pr_number} "
            f"(branch `{branch}` targeting `{base_branch}`).\n\n"
            f"The repository is `{repo}`. You are in the PR's worktree.\n\n"
            f"IMPORTANT: You have a maximum of 10 minutes to complete this review. "
            f"Be focused and concise. Prioritize the most impactful feedback.\n\n"
            f"CRITICAL: Your FINAL message will be posted as a GitHub PR comment. "
            f"It MUST contain the complete review with all findings. "
            f"Do not refer to previous messages or say 'above' — your final message must be self-contained."
        )

        proc = subprocess.Popen(
            [
                "claude",
                "-p", prompt,
                "--output-format", "stream-json",
                "--append-system-prompt", skill_content,
                "--dangerously-skip-permissions",
                "--max-turns", "50",
            ],
            cwd=worktree_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        try:
            try:
                claude_result = parse_stream_json(proc.stdout)
                proc.wait(timeout=600)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                raise
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

        stderr_output = proc.stderr.read() if proc.stderr else ""

        if proc.returncode != 0:
            log.error("Claude exited with code %d", proc.returncode)
            log.error("stderr: %s", stderr_output[:2000] if stderr_output else "(empty)")

        if claude_result.cost_usd is not None:
            log.info(
                "Claude: cost=$%.4f turns=%s session=%s",
                claude_result.cost_usd, claude_result.num_turns, claude_result.session_id,
            )

        if claude_result.is_error or proc.returncode != 0:
            output = select_review_output(claude_result) or stderr_output or "Claude exited with no output"
        else:
            output = select_review_output(claude_result)

        if not output.strip():
            output = "Review completed but produced no output."

        # 6. Post result as PR comment
        if not _killed:
            comment_url = upsert_comment(repo, pr_number, output, skill, head_sha)
            notify_review_posted(repo, pr_number, output, comment_url)

        log.info("Review complete for %s#%d", repo, pr_number)

    except subprocess.TimeoutExpired:
        log.error("Review timed out for %s#%d", repo, pr_number)
        if not _killed:
            upsert_comment(
                repo, pr_number,
                "**Claude Review Daemon Error**\n\nReview timed out after 10 minutes.",
                skill, head_sha,
            )
    except Exception as e:
        log.exception("Review failed for %s#%d: %s", repo, pr_number, e)
        if not _killed:
            upsert_comment(
                repo, pr_number,
                f"**Claude Review Daemon Error**\n\nReview failed: {type(e).__name__}",
                skill, head_sha,
            )
    finally:
        # 7. Clean up worktree
        if os.path.exists(worktree_path):
            log.info("Cleaning up worktree: %s", worktree_path)
            try:
                run(["git", "worktree", "remove", worktree_path, "--force"], cwd=repo_path)
            except Exception:
                log.warning("Failed to remove worktree %s", worktree_path, exc_info=True)


def truncate_output(output: str) -> str:
    if len(output) <= MAX_COMMENT_LENGTH:
        return output
    truncation_notice = "\n\n---\n*Output truncated (exceeded GitHub comment limit)*"
    return output[: MAX_COMMENT_LENGTH - len(truncation_notice)] + truncation_notice


def find_existing_comment(repo: str, pr_number: int, skill: str) -> int | None:
    marker = COMMENT_MARKER_TEMPLATE.format(skill=skill)
    try:
        result = subprocess.run(
            [
                "gh", "api",
                f"/repos/{repo}/issues/{pr_number}/comments",
                "--paginate", "-q",
                f'[.[] | select(.body | startswith("{marker}"))][0].id',
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0 and result.stdout.strip():
            comment_id = int(result.stdout.strip())
            log.info("Found existing comment %d for skill=%s", comment_id, skill)
            return comment_id
    except Exception:
        log.warning("Failed to search for existing comment", exc_info=True)
    return None


def upsert_comment(
    repo: str,
    pr_number: int,
    body: str,
    skill: str,
    head_sha: str | None = None,
) -> str | None:
    marker = COMMENT_MARKER_TEMPLATE.format(skill=skill)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    footer_parts = []
    if head_sha:
        footer_parts.append(f"Reviewed commit: `{head_sha[:7]}`")
    footer_parts.append(f"at {now}")
    footer = f"\n\n---\n*{' '.join(footer_parts)}*"

    full_body = f"{marker}\n{body}{footer}"
    full_body = truncate_output(full_body)

    existing_id = find_existing_comment(repo, pr_number, skill)
    if existing_id:
        log.info("Updating comment %d on %s#%d", existing_id, repo, pr_number)
        result = subprocess.run(
            [
                "gh", "api", "--method", "PATCH",
                f"/repos/{repo}/issues/comments/{existing_id}",
                "-f", f"body={full_body}",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            log.info("Comment updated successfully")
            try:
                return json.loads(result.stdout).get("html_url")
            except (json.JSONDecodeError, AttributeError):
                return None
        log.warning("Failed to update comment %d: %s — falling back to create", existing_id, result.stderr)

    return _create_comment(repo, pr_number, full_body)


def _create_comment(repo: str, pr_number: int, body: str) -> str | None:
    log.info("Creating comment on %s#%d (%d chars)", repo, pr_number, len(body))
    result = subprocess.run(
        [
            "gh", "pr", "comment", str(pr_number),
            "--repo", repo,
            "--body", body,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        log.error("Failed to create comment: %s", result.stderr)
        return None
    log.info("Comment created successfully")
    url = result.stdout.strip()
    return url if url.startswith("http") else None


def main():
    parser = argparse.ArgumentParser(description="Run a Claude review for a PR")
    parser.add_argument("--repo", required=True, help="GitHub repo (owner/name)")
    parser.add_argument("--pr-number", required=True, type=int, help="PR number")
    parser.add_argument("--branch", required=True, help="PR branch name")
    parser.add_argument("--base-branch", required=True, help="Target branch")
    parser.add_argument("--skill", default="review-pr", help="Skill name to execute")
    parser.add_argument("--repo-dir", required=True, help="Base directory for repos")
    parser.add_argument("--head-sha", help="Head commit SHA for tracking")
    args = parser.parse_args()

    run_review(
        repo=args.repo,
        pr_number=args.pr_number,
        branch=args.branch,
        base_branch=args.base_branch,
        skill=args.skill,
        repo_dir=args.repo_dir,
        head_sha=args.head_sha,
    )


if __name__ == "__main__":
    main()
