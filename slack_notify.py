"""Slack notification support for Claude Review Daemon."""

import json
import logging
import os
import re
import subprocess
from urllib.request import Request, urlopen

log = logging.getLogger("run-review")

SUMMARY_HEADINGS = re.compile(
    r"^##\s+(?:Summary|TL;DR|TLDR|Overview)\s*$", re.IGNORECASE
)


def extract_tldr(output: str, max_length: int = 300) -> str:
    lines = output.split("\n")

    # Try to find a summary/TLDR/overview section
    for i, line in enumerate(lines):
        if SUMMARY_HEADINGS.match(line.strip()):
            section_lines = []
            for subsequent in lines[i + 1 :]:
                if subsequent.strip().startswith("## "):
                    break
                section_lines.append(subsequent)
            text = "\n".join(section_lines).strip()
            if text:
                return _clean_and_truncate(text, max_length)

    # Fallback: first non-empty, non-heading, non-HTML-comment paragraph
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        if stripped.startswith("<!--") or stripped.startswith("-->"):
            continue
        return _clean_and_truncate(stripped, max_length)

    return ""


def _clean_and_truncate(text: str, max_length: int) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[*_`~]", "", text)
    text = text.strip()
    if len(text) <= max_length:
        return text
    truncated = text[:max_length].rsplit(" ", 1)[0]
    return truncated + "..."


def get_pr_title(repo: str, pr_number: int) -> str:
    try:
        result = subprocess.run(
            [
                "gh", "pr", "view", str(pr_number),
                "--repo", repo,
                "--json", "title", "-q", ".title",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        log.warning("Failed to fetch PR title for %s#%d", repo, pr_number, exc_info=True)
    return f"PR #{pr_number}"


def notify_review_posted(
    repo: str,
    pr_number: int,
    review_output: str,
    comment_url: str | None,
) -> None:
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook_url:
        return

    try:
        title = get_pr_title(repo, pr_number)
        tldr = extract_tldr(review_output)
        pr_url = f"https://github.com/{repo}/pull/{pr_number}"

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":mag: *Review posted: <{pr_url}|{title}>*\n`{repo}#{pr_number}`",
                },
            },
        ]

        if tldr:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*TL;DR:* {tldr}",
                },
            })

        if comment_url:
            blocks.append({
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "View Review"},
                        "url": comment_url,
                    }
                ],
            })

        payload = json.dumps({"blocks": blocks}).encode()
        req = Request(webhook_url, data=payload, headers={"Content-Type": "application/json"})
        urlopen(req, timeout=10)
        log.info("Slack notification sent for %s#%d", repo, pr_number)

    except Exception:
        log.warning("Failed to send Slack notification for %s#%d", repo, pr_number, exc_info=True)
