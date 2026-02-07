# claude-ci-bridge

A standalone tool that bridges GitHub PRs to Claude Max on a VPS, enabling any project to run Claude skills (e.g., PR review) via CI without API keys.

## How it works

```
                    Polling daemon (VPS)
                           |
          polls GitHub API every N minutes
                           |
               detects new PR commits
                           |
              1. git fetch origin
              2. git worktree add worktrees/pr-<N>
              3. Read .claude/commands/<skill>.md
              4. claude -p --append-system-prompt <skill>
              5. gh pr comment <N> --body <output>
              6. git worktree remove worktrees/pr-<N>
```

The daemon polls each configured repo for open PRs and triggers a review whenever it sees a commit it hasn't reviewed yet. No GitHub Actions workflow or webhooks required on the project side.

The tool is **project-agnostic**. Each project keeps its own `.claude/` config (skills, agents, `CLAUDE.md`). The bridge just orchestrates execution.

## Skill injection

Since slash commands (`/review-pr`) don't work in headless `-p` mode, the bridge:

1. Reads the skill definition from the project's `.claude/commands/<skill>.md`
2. Passes its content via `--append-system-prompt` to `claude -p`
3. Claude follows the instructions as if the skill was invoked interactively
4. The `.claude/agents/` directory is available in the worktree, so sub-agents work as expected

## Prerequisites

- **Python 3.10+** (stdlib only, no pip dependencies)
- **Claude CLI** authenticated with a Max subscription (`claude --version`)
- **GitHub CLI** authenticated (`gh auth status`)
- A VPS or server with network access to GitHub

## Quick start

### 1. Clone to the VPS

```bash
git clone https://github.com/your-org/claude-ci-bridge /opt/claude-ci-bridge
cd /opt/claude-ci-bridge
```

### 2. Create directories

```bash
mkdir -p repos logs
```

### 3. Clone your project repos

```bash
cd repos
git clone https://github.com/your-org/your-project your-org_your-project
```

The repo directory name must match `owner/name` with `/` replaced by `_`.

### 4. Configure

```bash
cp config.toml.example config.toml
cp .env.example .env
# Edit config.toml to add your repos
# Edit .env to set GH_TOKEN
```

### 5. Install systemd service

```bash
sudo cp claude-ci-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now claude-ci-bridge
```

See [docs/integration-guide.md](docs/integration-guide.md) for detailed setup and per-repo configuration.

## Configuration

### config.toml

```toml
[polling]
interval_seconds = 300      # Poll every 5 minutes
max_concurrent_reviews = 3  # Max simultaneous reviews

[[repos]]
name = "owner/repo"
skill = "review-pr"         # Optional, defaults to "review-pr"
branches = ["main"]         # Optional, filter by base branch
enabled = true
```

### Environment variables

| Variable | Description | Default |
|----------|-------------|---------|
| `GH_TOKEN` | GitHub PAT with repo scope | _(uses gh CLI auth)_ |
| `CONFIG_FILE` | Path to config.toml | `./config.toml` |

## Parallel reviews

Each PR gets its own git worktree (`worktrees/pr-<N>`), so multiple reviews can run simultaneously without interference. Worktrees are cleaned up after completion.

## Troubleshooting

**Skill file not found**: Ensure your project has `.claude/commands/<skill>.md` committed to the PR branch.

**Claude produces no output**: Check that the Claude CLI is authenticated (`claude --version`) and the Max subscription is active.

**Worktree conflicts**: If a previous review crashed, stale worktrees are automatically cleaned up on the next run for the same PR.
