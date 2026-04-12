# ai-code-reviewer

Automated AI code review for every Pull Request, powered by Anthropic Claude.

Add one file to any repository and every new PR will automatically receive:
- A **summary comment** with per-file ratings (good / needs work / critical)
- **Inline review comments** on specific lines in the diff

---

## How it works

```
Developer opens PR
        |
        v
Caller repo workflow triggers (pull_request event)
        |
        v
Calls nidhinsai/ai-code-reviewer/.github/workflows/pr-review.yml
        |
        v
Fetches PR diff from GitHub API
        |
        v
Sends each changed file + diff to Claude (claude-3-5-sonnet)
        |
        v
Posts summary comment + inline review comments on the PR
```

---

## Setup (2 steps)

### Step 1 — Add your Anthropic API key

In each target repository (or at the org level):
Settings -> Secrets and variables -> Actions -> New repository secret

| Name                | Value                  |
|---------------------|------------------------|
| ANTHROPIC_API_KEY   | Your Anthropic API key |

### Step 2 — Add the caller workflow

Create `.github/workflows/pr-review.yml` in each target repository:

```yaml
name: AI PR Code Review

on:
  pull_request:
    types: [opened, synchronize, reopened]

jobs:
  ai-review:
    uses: nidhinsai/ai-code-reviewer/.github/workflows/pr-review.yml@main
    with:
      pr_number: ${{ github.event.pull_request.number }}
      repository: ${{ github.repository }}
    secrets:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
    permissions:
      pull-requests: write
      contents: read
```

A ready-to-copy template is at `templates/pr-review-caller.yml`.

---

## What gets reviewed

Included: .java .py .ts .js .go .rs .kt .swift .c .cpp .cs and all other code files
Skipped:  .md .yml .json .lock .xml .toml images fonts binaries

---

## Repository structure

```
ai-code-reviewer/
├── .github/
│   └── workflows/
│       └── pr-review.yml         <- Reusable workflow (call from other repos)
├── scripts/
│   └── review.py                 <- Claude review agent
├── templates/
│   └── pr-review-caller.yml      <- Copy this to your target repos
├── requirements.txt
└── README.md
```

---

## Configuration

| Environment variable | Default                         | Description                                   |
|----------------------|---------------------------------|-----------------------------------------------|
| CLAUDE_MODEL         | claude-3-5-sonnet-20241022      | Claude model to use                           |
| GH_PAT               | (optional)                      | GitHub PAT if you need explicit write access  |
