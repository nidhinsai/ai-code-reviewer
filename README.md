# ai-code-reviewer

Automated AI code review for every Pull Request, powered by **GitHub Models (gpt-4o)**.

> **No API keys required.**
> Uses `GITHUB_TOKEN` — auto-injected in every Actions run — to call GitHub Models
> via your Copilot subscription.

Every new PR gets:
- A **summary comment** with per-file ratings (good / needs work / critical)
- **Inline review comments** pinned to specific lines in the diff

---

## How it works

```
Developer opens PR
        |
        v
Caller repo .github/workflows/pr-review.yml triggers
        |
        v
Calls nidhinsai/ai-code-reviewer/.github/workflows/pr-review.yml (reusable workflow)
        |
        v
Fetches PR diff from GitHub API
        |
        v
Sends each changed file + diff to GitHub Models (gpt-4o)
via GITHUB_TOKEN — no Anthropic / OpenAI key needed
        |
        v
Posts summary + inline review comments on the PR
```

---

## Setup — 1 step only

Create `.github/workflows/pr-review.yml` in each repository you want reviewed:

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
    permissions:
      pull-requests: write
      contents: read
```

A ready-to-copy template is at `templates/pr-review-caller.yml`.

No secrets to add. `GITHUB_TOKEN` is injected automatically and grants access to
GitHub Models via your Copilot subscription.

---

## What gets reviewed

| Reviewed | Skipped |
|----------|---------|
| `.java` `.py` `.ts` `.js` `.go` `.rs` `.kt` `.swift` `.c` `.cpp` `.cs` and all other code files | `.md` `.yml` `.json` `.lock` `.xml` `.toml` images fonts binaries |

---

## Repository structure

```
ai-code-reviewer/
├── .github/
│   └── workflows/
│       └── pr-review.yml         <- Reusable workflow (call this from other repos)
├── scripts/
│   └── review.py                 <- Review agent (GitHub Models / gpt-4o)
├── templates/
│   └── pr-review-caller.yml      <- Copy this file into your target repos
├── requirements.txt
└── README.md
```

---

## Configuration

| Environment variable | Default  | Description                                    |
|----------------------|----------|------------------------------------------------|
| `REVIEW_MODEL`       | `gpt-4o` | GitHub Models model to use                     |
| `GH_PAT`             | optional | PAT only if you need explicit cross-repo access|

Other available GitHub Models (Copilot subscription required):
- `gpt-4o` — default, best quality
- `gpt-4o-mini` — faster, lower rate-limit usage
- `o1-mini`
