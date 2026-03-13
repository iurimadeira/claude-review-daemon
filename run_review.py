#!/usr/bin/env python3
"""Review execution logic for Claude Review Daemon.

Manages git worktrees, runs Claude with skill injection, and posts
results back to GitHub as PR reviews with inline comments.
"""

import argparse
import json
import logging
import os
import re
import signal as _signal
import subprocess
import sys
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


@dataclass
class ReviewOutput:
    summary: str
    event: str
    comments: list[dict]


_VALID_REVIEW_EVENTS = {"APPROVE", "REQUEST_CHANGES"}


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


def parse_review_json(text: str) -> ReviewOutput | None:
    raw = text.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        raw = "\n".join(lines)
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    summary = data.get("summary")
    event = data.get("event", "REQUEST_CHANGES")
    comments = data.get("comments", [])
    if not isinstance(summary, str) or not summary.strip():
        return None
    if event not in _VALID_REVIEW_EVENTS:
        event = "REQUEST_CHANGES"
    valid_comments = []
    for c in comments:
        if not isinstance(c, dict) or "path" not in c or "body" not in c:
            continue
        line = c.get("line")
        try:
            line = int(line)
        except (TypeError, ValueError):
            continue
        if line < 1:
            continue
        valid_comments.append(
            {"path": str(c["path"]), "line": int(line), "body": str(c["body"])}
        )
    return ReviewOutput(summary=summary.strip(), event=event, comments=valid_comments)


_MD_INLINE_PATTERN = re.compile(
    r"\*\*`([^`]+)`\s*\(lines?\s+(\d+)\)\*\*\s*\n((?:(?!\*\*`).)*)",
    re.DOTALL,
)


def parse_review_markdown(text: str) -> ReviewOutput | None:
    comments = []
    for m in _MD_INLINE_PATTERN.finditer(text):
        path, line_str, body = m.group(1), m.group(2), m.group(3).strip()
        try:
            line = int(line_str)
        except (TypeError, ValueError):
            continue
        if line < 1 or not body:
            continue
        comments.append({"path": path, "line": line, "body": body})

    if not comments:
        return None

    first_match = _MD_INLINE_PATTERN.search(text)
    summary_text = text[:first_match.start()].strip() if first_match else ""

    summary_lines = []
    for line in summary_text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("<!--"):
            continue
        if stripped == "---":
            continue
        summary_lines.append(line)
    summary = "\n".join(summary_lines).strip()

    if not summary:
        summary = f"Review with {len(comments)} inline comment(s)."

    event = "REQUEST_CHANGES" if comments else "APPROVE"

    return ReviewOutput(summary=summary, event=event, comments=comments)


def format_review_as_comment(review: ReviewOutput) -> str:
    parts = [review.summary]
    for c in review.comments:
        parts.append(f"\n**`{c['path']}` (line {c['line']})**\n{c['body']}")
    return "\n".join(parts)


MAX_COMMENT_LENGTH = 65000  # GitHub comment limit is 65536
COMMENT_MARKER_TEMPLATE = "<!-- claude-review-daemon:{skill} -->"

_JSON_FORMAT_INSTRUCTIONS = (
    "\n\n--- OUTPUT FORMAT (OVERRIDES ALL OTHER FORMAT INSTRUCTIONS) ---\n\n"
    "CRITICAL: Your FINAL message MUST be a raw JSON object "
    "(no markdown code fences, no extra text before or after) with this exact structure:\n"
    '{"summary": "...", "event": "APPROVE", '
    '"comments": [{"path": "src/file.py", "line": 42, "body": "Your finding"}]}\n\n'
    "Rules:\n"
    "- summary: A concise, high-level opinion about the PR as a whole. "
    "Express whether the approach is sound, the code quality, and your overall impression. "
    "Do NOT list specific findings here — those go in the comments array.\n"
    "- event: MUST be APPROVE or REQUEST_CHANGES. "
    "Use APPROVE if the code is good (minor suggestions OK). "
    "Use REQUEST_CHANGES if there are issues to fix before merging.\n"
    "- comments: Each specific finding as an inline comment. "
    "EVERY comment MUST have a numeric line number (integer, not null) "
    "from the actual file. path is relative to repo root. Markdown supported in body.\n"
    "- Ignore any other output format instructions in the skill above. "
    "This JSON format is mandatory."
)


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
            post_comment(repo, pr_number, f"**Claude Review Daemon Error**\n\n{error_msg}", skill, head_sha)
            return

        with open(skill_path) as f:
            skill_content = f.read()

        log.info("Loaded skill file: %s (%d bytes)", skill_path, len(skill_content))

        # 5. Fetch existing review comments to avoid duplicate findings
        existing_comments = fetch_existing_review_comments(repo, pr_number)
        existing_context = ""
        if existing_comments:
            lines = []
            for ec in existing_comments:
                path = ec.get("path", "?")
                line = ec.get("line", "?")
                body = ec.get("body", "").replace("\n", " ")[:200]
                lines.append(f"- {path}:{line} — {body}")
            existing_context = (
                f"\n\nEXISTING REVIEW COMMENTS (do NOT repeat these findings):\n"
                + "\n".join(lines)
            )

        # 6. Run Claude with skill injection
        prompt = (
            f"Execute the following skill for PR #{pr_number} "
            f"(branch `{branch}` targeting `{base_branch}`).\n\n"
            f"The repository is `{repo}`. You are in the PR's worktree.\n\n"
            f"IMPORTANT: You have a maximum of 10 minutes to complete this review. "
            f"Be focused and concise. Prioritize the most impactful feedback."
            f"{existing_context}"
        )

        system_prompt = skill_content + _JSON_FORMAT_INSTRUCTIONS

        proc = subprocess.Popen(
            [
                "claude",
                "-p", prompt,
                "--verbose",
                "--output-format", "stream-json",
                "--append-system-prompt", system_prompt,
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

        # 7. Parse review output (JSON first, then markdown fallback)
        review = None
        output = ""

        if claude_result.is_error or proc.returncode != 0:
            output = select_review_output(claude_result) or stderr_output or "Claude exited with no output"
        else:
            review = parse_review_json(claude_result.result_text)
            if review:
                log.info("Parsed review JSON from result_text")
            else:
                output = select_review_output(claude_result)
                review = parse_review_json(output)
                if review:
                    log.info("Parsed review JSON from selected output")
                else:
                    review = parse_review_markdown(output)
                    if review:
                        log.info("Parsed review from markdown format (%d comments)", len(review.comments))

        if not review and not output.strip():
            output = "Review completed but produced no output."

        # 8. Post result as PR review or comment
        if not _killed:
            if review and head_sha:
                old_comments = find_existing_comments(repo, pr_number, skill)
                for comment in old_comments:
                    if minimize_comment(comment["node_id"]):
                        log.info("Minimized old comment %s", comment["id"])
                review_url = create_review(repo, pr_number, head_sha, review, skill)
                if review_url:
                    notify_review_posted(repo, pr_number, review.summary, review_url)
                else:
                    formatted = format_review_as_comment(review)
                    url = post_comment(repo, pr_number, formatted, skill, head_sha)
                    notify_review_posted(repo, pr_number, review.summary, url)
            else:
                url = post_comment(repo, pr_number, output, skill, head_sha)
                notify_review_posted(repo, pr_number, output, url)

        log.info("Review complete for %s#%d", repo, pr_number)

    except subprocess.TimeoutExpired:
        log.error("Review timed out for %s#%d", repo, pr_number)
        if not _killed:
            post_comment(
                repo, pr_number,
                "**Claude Review Daemon Error**\n\nReview timed out after 10 minutes.",
                skill, head_sha,
            )
    except Exception as e:
        log.exception("Review failed for %s#%d: %s", repo, pr_number, e)
        if not _killed:
            post_comment(
                repo, pr_number,
                f"**Claude Review Daemon Error**\n\nReview failed: {type(e).__name__}",
                skill, head_sha,
            )
    finally:
        # 9. Clean up worktree
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


def fetch_existing_review_comments(repo: str, pr_number: int) -> list[dict]:
    try:
        result = subprocess.run(
            [
                "gh", "api",
                f"/repos/{repo}/pulls/{pr_number}/comments",
                "--paginate", "-q",
                '[.[] | {path: .path, line: (.line // .original_line), body: .body}]',
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0 and result.stdout.strip():
            comments = json.loads(result.stdout)
            log.info("Found %d existing review comment(s) on PR", len(comments))
            return comments
    except Exception:
        log.warning("Failed to fetch existing review comments", exc_info=True)
    return []


def find_existing_comments(repo: str, pr_number: int, skill: str) -> list[dict]:
    marker = COMMENT_MARKER_TEMPLATE.format(skill=skill)
    try:
        result = subprocess.run(
            [
                "gh", "api",
                f"/repos/{repo}/issues/{pr_number}/comments",
                "--paginate", "-q",
                f'[.[] | select(.body | startswith("{marker}")) | {{id: .id, node_id: .node_id}}]',
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0 and result.stdout.strip():
            comments = json.loads(result.stdout)
            log.info("Found %d existing comment(s) for skill=%s", len(comments), skill)
            return comments
    except Exception:
        log.warning("Failed to search for existing comments", exc_info=True)
    return []


def minimize_comment(node_id: str) -> bool:
    query = (
        "mutation($id: ID!) {"
        "  minimizeComment(input: {subjectId: $id, classifier: OUTDATED}) {"
        "    minimizedComment { isMinimized }"
        "  }"
        "}"
    )
    try:
        result = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={query}", "-f", f"id={node_id}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return True
        log.warning("Failed to minimize comment %s: %s", node_id, result.stderr)
    except Exception:
        log.warning("Failed to minimize comment %s", node_id, exc_info=True)
    return False


def fetch_diff_lines(repo: str, pr_number: int) -> dict[str, set[int]]:
    try:
        result = subprocess.run(
            ["gh", "api", f"/repos/{repo}/pulls/{pr_number}/files", "--paginate"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            return {}
        files = json.loads(result.stdout)
        diff_lines: dict[str, set[int]] = {}
        for f in files:
            filename = f.get("filename", "")
            patch = f.get("patch", "")
            if not patch:
                continue
            lines = set()
            current_line = 0
            for patch_line in patch.split("\n"):
                if patch_line.startswith("@@"):
                    match = re.search(r"\+(\d+)", patch_line)
                    if match:
                        current_line = int(match.group(1))
                    continue
                if patch_line.startswith("-"):
                    continue
                if patch_line.startswith("+") or not patch_line.startswith("\\"):
                    lines.add(current_line)
                    current_line += 1
            diff_lines[filename] = lines
        return diff_lines
    except Exception:
        log.warning("Failed to fetch diff lines", exc_info=True)
        return {}


def _submit_review(repo: str, pr_number: int, payload: dict) -> str | None:
    result = subprocess.run(
        [
            "gh", "api", f"repos/{repo}/pulls/{pr_number}/reviews",
            "--method", "POST", "--input", "-",
        ],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        log.error("Failed to create review: %s %s", result.stderr, result.stdout[:500] if result.stdout else "")
        return None
    log.info("Review created successfully")
    try:
        return json.loads(result.stdout).get("html_url")
    except (json.JSONDecodeError, KeyError):
        return None


def create_review(
    repo: str,
    pr_number: int,
    head_sha: str,
    review: ReviewOutput,
    skill: str,
) -> str | None:
    marker = COMMENT_MARKER_TEMPLATE.format(skill=skill)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body = f"{marker}\n{review.summary}\n\n---\n*Reviewed commit: `{head_sha[:7]}` at {now}*"

    # Filter comments to only include lines that are in the diff
    diff_lines = fetch_diff_lines(repo, pr_number)
    valid_comments = []
    for c in review.comments:
        valid_lines = diff_lines.get(c["path"], set())
        if valid_lines and c["line"] in valid_lines:
            valid_comments.append(c)
        else:
            log.info("Skipping comment on %s:%d (not in diff)", c["path"], c["line"])

    payload = {
        "commit_id": head_sha,
        "body": body,
        "event": review.event,
        "comments": [
            {"path": c["path"], "line": c["line"], "side": "RIGHT", "body": c["body"]}
            for c in valid_comments
        ],
    }

    log.info(
        "Creating review on %s#%d (event=%s, %d inline comments, %d skipped)",
        repo, pr_number, review.event, len(valid_comments),
        len(review.comments) - len(valid_comments),
    )

    url = _submit_review(repo, pr_number, payload)
    if url:
        return url

    # APPROVE/REQUEST_CHANGES fail on own PRs; fall back to COMMENT
    if payload["event"] != "COMMENT":
        log.warning("Retrying review with COMMENT event (own PR restriction)")
        payload["event"] = "COMMENT"
        url = _submit_review(repo, pr_number, payload)
        if url:
            return url

    # If still failing with comments, retry without them
    if payload["comments"]:
        log.warning("Retrying review without inline comments")
        payload["comments"] = []
        return _submit_review(repo, pr_number, payload)

    return None


def post_comment(
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

    old_comments = find_existing_comments(repo, pr_number, skill)
    for comment in old_comments:
        if minimize_comment(comment["node_id"]):
            log.info("Minimized old review comment %s", comment["id"])

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
