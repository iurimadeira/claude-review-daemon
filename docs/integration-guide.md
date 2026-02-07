# Integration Guide

How to add your project to claude-ci-bridge for automatic PR reviews.

## Overview

The bridge polls GitHub for open PRs and triggers reviews when:
- A new PR is opened
- New commits are pushed to an existing PR

No GitHub Actions workflow or webhooks required on the project side.

## 1. Prerequisites

Your project needs:
- A `.claude/commands/<skill>.md` file defining the review skill
- The project repo cloned on the bridge server

## 2. Create or select a skill file

The bridge executes the skill file in `.claude/commands/<skill-name>.md`.

A good CI skill file:
- Gives Claude clear instructions on what to review
- References any agents or configs in `.claude/agents/` that Claude should use
- Specifies the output format (markdown for PR comments)
- Is self-contained — Claude won't have interactive context

Example structure:

```markdown
You are reviewing a pull request. Use the following approach:

1. Run `gh pr diff` to see the changes
2. Analyze the code for [your criteria]
3. Output a structured review as markdown

You may use the Task tool to spawn sub-agents from `.claude/agents/` as needed.
```

## 3. Clone the repo on the bridge server

```bash
cd /opt/claude-ci-bridge/repos
git clone https://github.com/owner/repo owner_repo
```

The directory name must be `owner_repo` (slash replaced with underscore).

## 4. Add the repo to config.toml

Edit `/opt/claude-ci-bridge/config.toml`:

```toml
[[repos]]
name = "owner/repo"
skill = "review-pr"      # Optional, defaults to "review-pr"
branches = ["main"]      # Optional, filter by base branch
enabled = true
```

The daemon will pick up the change on the next poll cycle.

## 5. Verify

1. **Check the daemon logs**: `journalctl -u claude-ci-bridge -f`
2. **Open a test PR** in your project
3. **Wait for the review comment** (up to polling interval + review time)

### Troubleshooting

**No review triggers**:
- Check the repo is in config.toml and enabled
- Verify the repo is cloned: `ls /opt/claude-ci-bridge/repos/owner_repo`
- Check logs for errors: `journalctl -u claude-ci-bridge | grep owner/repo`

**Review fails with "Skill file not found"**:
- Ensure `.claude/commands/<skill>.md` exists on the PR branch

**Review times out**:
- Check Claude is running: `which claude`
- Verify `gh` is authenticated: `gh auth status`

## Configuration Reference

### config.toml

```toml
[polling]
interval_seconds = 300      # Poll every 5 minutes
max_concurrent_reviews = 3  # Max simultaneous reviews

[paths]
state_file = "./state.json"
repo_dir = "./repos"

[[repos]]
name = "owner/repo"         # Required: GitHub repo
skill = "review-pr"         # Optional: skill file name
branches = ["main"]         # Optional: only PRs targeting these
enabled = true              # Optional: set false to pause
```

### Environment Variables

| Variable | Description |
|----------|-------------|
| `GH_TOKEN` | GitHub PAT with repo scope |
| `CONFIG_FILE` | Path to config.toml (default: `./config.toml`) |

## Rate Limits

The bridge uses GitHub's REST API with ETags for efficient polling:
- Authenticated: 5,000 requests/hour
- 304 responses (no changes) don't count against quota
- 10 repos at 5-minute intervals ≈ 120 requests/hour

## State File

The bridge tracks reviewed commits in `state.json`:

```json
{
  "version": 1,
  "repos": {
    "owner/repo": {
      "etag": "\"abc123\"",
      "prs": {
        "42": {
          "head_sha": "deadbeef...",
          "reviewed_at": "2024-01-15T10:25:00Z",
          "review_status": "completed"
        }
      }
    }
  }
}
```

On first run, all open PRs are reviewed. Closed PRs are automatically cleaned from state.
