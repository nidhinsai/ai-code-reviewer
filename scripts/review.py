#!/usr/bin/env python3
"""
AI PR Analysis Agent — ai-code-reviewer
Combines two capabilities originally in separate bots:

1. PR SUMMARIZATION (ported from Summarize-Pull-Request / flows.network bot)
   - Fetches every commit in the PR and its diff
   - Asks gpt-4o to summarize each commit: key changes + potential problems
   - If >1 commit: second LLM pass synthesises an overall summary
   - Posts per-commit breakdown with GitHub commit links

2. PER-FILE CODE REVIEW
   - Reviews every changed code file's diff
   - Rates each file: good / needs_work / critical
   - Posts inline review comments pinned to specific diff lines

TRIGGERS:
   - pull_request events (opened, synchronize, reopened)
   - issue_comment events containing the magic phrase "ai summarize"
     (re-runs the full analysis and updates the existing bot comment)

All powered by GitHub Models (gpt-4o) via GITHUB_TOKEN — no API key needed.
Works with any GitHub Copilot subscription.
"""

import json
import os
import re
import sys

import requests
from openai import OpenAI

# ── Environment ───────────────────────────────────────────────────────────────
GH_TOKEN     = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
PR_NUMBER    = int(os.environ.get("PR_NUMBER", "0"))
REPO         = os.environ.get("REPO", "")
MODEL        = os.environ.get("REVIEW_MODEL", "gpt-4o")
EVENT_NAME   = os.environ.get("EVENT_NAME", "pull_request")   # pull_request | issue_comment
COMMENT_BODY = os.environ.get("COMMENT_BODY", "")             # body of the triggering comment
ISSUE_NUMBER = int(os.environ.get("ISSUE_NUMBER") or "0")     # for issue_comment trigger

TRIGGER_PHRASE = "ai summarize"   # magic comment phrase to re-trigger

GH_MODELS_URL = "https://models.inference.ai.azure.com"

# Extensions skipped for file-level code review (still included in commit diffs)
SKIP_EXTENSIONS = {
    ".md", ".txt", ".lock", ".sum", ".json", ".yaml", ".yml", ".toml",
    ".xml", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".pdf",
    ".zip", ".tar", ".gz", ".woff", ".woff2", ".ttf", ".eot",
}

# Soft character limit per commit diff sent to the model (mirrors original bot)
CHAR_SOFT_LIMIT = 9000

GH_API = "https://api.github.com"
GH_HEADERS = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

client = OpenAI(base_url=GH_MODELS_URL, api_key=GH_TOKEN)

# ── GitHub helpers ────────────────────────────────────────────────────────────

def gh_get(path, params=None, accept=None):
    headers = dict(GH_HEADERS)
    if accept:
        headers["Accept"] = accept
    r = requests.get(f"{GH_API}{path}", headers=headers, params=params, timeout=20)
    r.raise_for_status()
    return r

def gh_get_json(path, params=None):
    return gh_get(path, params=params).json()

def get_pr_info():
    return gh_get_json(f"/repos/{REPO}/pulls/{PR_NUMBER}")

def get_pr_files():
    all_files, page = [], 1
    while True:
        batch = gh_get_json(f"/repos/{REPO}/pulls/{PR_NUMBER}/files",
                            params={"per_page": 100, "page": page})
        if not batch:
            break
        all_files.extend(batch)
        page += 1
    return all_files

def get_pr_commits():
    return gh_get_json(f"/repos/{REPO}/pulls/{PR_NUMBER}/commits",
                       params={"per_page": 100})

def get_commit_diff(sha):
    """Return the unified diff text for a single commit."""
    r = gh_get(f"/repos/{REPO}/commits/{sha}",
               accept="application/vnd.github.diff")
    return r.text

def get_existing_bot_comment():
    comments = gh_get_json(f"/repos/{REPO}/issues/{PR_NUMBER}/comments",
                           params={"per_page": 100})
    for c in comments:
        if "<!-- ai-code-reviewer -->" in c.get("body", ""):
            return c["id"]
    return None

def post_or_update_comment(body):
    marker    = "<!-- ai-code-reviewer -->"
    full_body = f"{marker}\n{body}"
    existing_id = get_existing_bot_comment()
    if existing_id:
        r = requests.patch(f"{GH_API}/repos/{REPO}/issues/comments/{existing_id}",
                           headers=GH_HEADERS, json={"body": full_body}, timeout=20)
    else:
        r = requests.post(f"{GH_API}/repos/{REPO}/issues/{PR_NUMBER}/comments",
                          headers=GH_HEADERS, json={"body": full_body}, timeout=20)
    r.raise_for_status()

def post_review(summary_body, inline_comments):
    marker  = "<!-- ai-code-reviewer -->"
    payload = {"body": f"{marker}\n{summary_body}", "event": "COMMENT"}
    if inline_comments:
        payload["comments"] = inline_comments
    r = requests.post(f"{GH_API}/repos/{REPO}/pulls/{PR_NUMBER}/reviews",
                      headers=GH_HEADERS, json=payload, timeout=30)
    if r.status_code not in (200, 201):
        print(f"  Review API {r.status_code}; falling back to plain comment.")
        post_or_update_comment(summary_body)
    else:
        print(f"  Review posted ({len(inline_comments)} inline comment(s)).")

def map_file_lines_to_diff_positions(patch: str) -> dict:
    mapping  = {}
    diff_pos = 0
    new_line = 0
    for raw in patch.split("\n"):
        if raw.startswith("@@"):
            diff_pos += 1
            m = re.search(r"\+(\d+)", raw)
            if m:
                new_line = int(m.group(1)) - 1
        elif raw.startswith("+"):
            diff_pos += 1
            new_line += 1
            mapping[new_line] = diff_pos
        elif raw.startswith("-"):
            diff_pos += 1
        elif not raw.startswith("\\"):
            diff_pos += 1
            new_line += 1
            mapping[new_line] = diff_pos
    return mapping

def truncate(s, max_chars=CHAR_SOFT_LIMIT):
    return s[:max_chars]

# ── LLM helpers ───────────────────────────────────────────────────────────────

def llm(system, user, max_tokens=1024, temperature=0.2):
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": system},
                  {"role": "user",   "content": user}],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return response.choices[0].message.content.strip()

# ── 1. PR SUMMARIZATION (ported from Summarize-Pull-Request) ─────────────────

COMMIT_SUMMARY_SYSTEM = (
    "You are an experienced software developer acting as a code reviewer for a GitHub Pull Request. "
    "You write concise, precise summaries. Focus on what changed, why it matters, and any potential problems."
)

OVERALL_SUMMARY_SYSTEM = (
    "You are an experienced software developer. "
    "You synthesise multiple code-review summaries into one cohesive overview. "
    "Lead with potential issues and errors, then cover the most important findings."
)

def summarize_commit(sha, message, diff, pr_title):
    question = (
        f"The following is a GitHub commit diff from a PR titled \"{pr_title}\".\n"
        f"Commit message: {message}\n\n"
        "Please summarize the key changes and identify potential problems. "
        "Start with the most important findings.\n\n"
        + truncate(diff)
    )
    return llm(COMMIT_SUMMARY_SYSTEM, question, max_tokens=512)

def synthesize_all_summaries(summaries_text, pr_title):
    question = (
        f"Below are individual summaries of commits in a PR titled \"{pr_title}\". "
        "Each summary is separated by ------.\n"
        "Write an overall summary. Present potential issues and errors first, "
        "then the most important findings.\n\n"
        + truncate(summaries_text)
    )
    return llm(OVERALL_SUMMARY_SYSTEM, question, max_tokens=768)

def generate_pr_summary(pr_title):
    commits = get_pr_commits()
    print(f"  Commits in PR: {len(commits)}")
    if not commits:
        return "No commits found in this PR."

    per_commit = []      # (sha, short_sha, message, summary_text)
    summaries_concat = ""

    for c in commits:
        sha     = c["sha"]
        short   = sha[:7]
        message = c["commit"]["message"].split("\n")[0]
        print(f"  Summarising commit {short}: {message[:60]}")
        try:
            diff = get_commit_diff(sha)
        except Exception as e:
            print(f"    diff fetch error: {e}")
            diff = ""
        if not diff.strip():
            summary = "(no diff available)"
        else:
            try:
                summary = summarize_commit(sha, message, diff, pr_title)
            except Exception as e:
                print(f"    LLM error: {e}")
                summary = f"(summarization failed: {e})"
        per_commit.append((sha, short, message, summary))
        summaries_concat += f"------\n{summary}\n"

    # Build section markdown
    lines = []

    # Overall synthesis if >1 commit
    if len(per_commit) > 1:
        print("  Generating overall synthesis...")
        try:
            overall = synthesize_all_summaries(summaries_concat, pr_title)
        except Exception as e:
            overall = f"(overall synthesis failed: {e})"
        lines.append("## \U0001f4cb PR Summary\n")
        lines.append(overall)
        lines.append("\n---\n")
        lines.append("### \U0001f501 Commit-by-Commit Breakdown\n")
    else:
        lines.append("## \U0001f4cb PR Summary\n")

    for sha, short, message, summary in per_commit:
        commit_url = f"https://github.com/{REPO}/pull/{PR_NUMBER}/commits/{sha}"
        lines.append(f"#### [\u2937 `{short}`]({commit_url}) — {message}\n")
        lines.append(summary)
        lines.append("")

    return "\n".join(lines)

# ── 2. PER-FILE CODE REVIEW ───────────────────────────────────────────────────

CODE_REVIEW_SYSTEM = (
    "You are a senior software engineer and code reviewer. "
    "You write precise, actionable review comments. "
    "Focus on correctness, security, performance, and maintainability. "
    "Never add unnecessary praise. Never comment on style unless it causes bugs."
)

REVIEW_PROMPT = """\
PR Title: {pr_title}
File: {filename}  (status: {status})

Unified diff:
```
{patch}
```
{file_section}

Review this change. Respond with ONLY a valid JSON object:

{{
  "summary": "1-3 sentences: what does this change do and is it correct?",
  "rating": "good" | "needs_work" | "critical",
  "inline_comments": [
    {{
      "line": <integer: line number in the NEW version of the file>,
      "severity": "critical" | "warning" | "suggestion",
      "comment": "Specific, actionable feedback"
    }}
  ]
}}

Rules:
- Only reference lines present in the diff (added or context lines).
- Only flag real issues: bugs, security flaws, logic errors, performance problems.
- If the change is clean, return rating "good" and an empty inline_comments array.
"""

def review_file(filename, patch, file_content, pr_title, status):
    file_section = ""
    if file_content:
        file_section = f"\nFull file (may be truncated):\n```\n{file_content[:5000]}\n```"
    prompt = REVIEW_PROMPT.format(
        pr_title=pr_title, filename=filename, status=status,
        patch=patch[:4000], file_section=file_section,
    )
    text = llm(CODE_REVIEW_SYSTEM, prompt, max_tokens=1024, temperature=0.2)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {"summary": text, "rating": "needs_work", "inline_comments": []}
    try:
        return json.loads(m.group())
    except json.JSONDecodeError:
        return {"summary": text, "rating": "needs_work", "inline_comments": []}

def generate_code_review(pr_title, pr_files):
    reviewable = [
        f for f in pr_files
        if f.get("patch")
        and os.path.splitext(f["filename"])[1].lower() not in SKIP_EXTENSIONS
    ]
    print(f"  Code files to review: {len(reviewable)}")

    summary_sections    = []
    all_inline_comments = []

    for f in reviewable:
        filename = f["filename"]
        patch    = f["patch"]
        status   = f.get("status", "modified")
        print(f"  -> {filename}")

        file_content = ""
        if f.get("raw_url") and status != "removed":
            try:
                cr = requests.get(f["raw_url"], headers=GH_HEADERS, timeout=10)
                if cr.status_code == 200:
                    file_content = cr.text
            except Exception as e:
                print(f"    (content fetch error: {e})")

        try:
            review = review_file(filename, patch, file_content, pr_title, status)
        except Exception as e:
            print(f"    Model error: {e}")
            continue

        rating = review.get("rating", "needs_work")
        icon   = {"good": "\u2705", "needs_work": "\u26a0\ufe0f", "critical": "\U0001f534"}.get(rating, "\u2139\ufe0f")
        summary_sections.append(f"### {icon} `{filename}`\n{review.get('summary', 'No summary.')}")

        position_map = map_file_lines_to_diff_positions(patch)
        for ic in review.get("inline_comments", []):
            line_num = ic.get("line")
            diff_pos = position_map.get(line_num)
            if diff_pos is None and position_map:
                nearest  = min(position_map.keys(), key=lambda ln: abs(ln - (line_num or 0)))
                diff_pos = position_map[nearest]
            if diff_pos is None:
                continue
            sev      = ic.get("severity", "suggestion")
            sev_icon = {"critical": "\U0001f534", "warning": "\U0001f7e1", "suggestion": "\U0001f535"}.get(sev, "\u2139\ufe0f")
            all_inline_comments.append({
                "path": filename, "position": diff_pos,
                "body": f"{sev_icon} **{sev.title()}**: {ic.get('comment', '')}",
            })

    if not summary_sections:
        review_md = (
            "## \U0001f50d Code Review\n\n"
            "No reviewable code changes found (only config/docs files changed)."
        )
    else:
        good = sum(1 for s in summary_sections if "\u2705" in s)
        warn = sum(1 for s in summary_sections if "\u26a0" in s)
        crit = sum(1 for s in summary_sections if "\U0001f534" in s)
        review_md = (
            f"## \U0001f50d Code Review\n\n"
            f"> Reviewed **{len(summary_sections)}** file(s) \u2014 "
            f"\u2705 {good} good \u00b7 \u26a0\ufe0f {warn} need work \u00b7 \U0001f534 {crit} critical\n\n"
            f"---\n\n" + "\n\n".join(summary_sections)
        )

    return review_md, all_inline_comments

# ── Main ──────────────────────────────────────────────────────────────────────

def resolve_pr_from_issue():
    """When triggered via issue_comment, look up the PR number from the issue number."""
    global PR_NUMBER
    if PR_NUMBER != 0:
        return True
    if ISSUE_NUMBER == 0:
        print("ERROR: Cannot determine PR number.", file=sys.stderr)
        return False
    # Check if issue is actually a PR
    data = gh_get_json(f"/repos/{REPO}/pulls/{ISSUE_NUMBER}")
    if data:
        PR_NUMBER = ISSUE_NUMBER
        return True
    print(f"Issue #{ISSUE_NUMBER} is not a PR.", file=sys.stderr)
    return False

def main():
    if not GH_TOKEN or not REPO:
        print("ERROR: GH_TOKEN and REPO are required.", file=sys.stderr)
        sys.exit(1)

    # Handle magic trigger phrase via issue_comment
    if EVENT_NAME == "issue_comment":
        if TRIGGER_PHRASE not in COMMENT_BODY.lower():
            print(f"Comment does not contain trigger phrase '{TRIGGER_PHRASE}'. Skipping.")
            return
        if not resolve_pr_from_issue():
            sys.exit(1)
        print(f"Magic trigger detected — re-running analysis on PR #{PR_NUMBER}")
    elif PR_NUMBER == 0:
        print("ERROR: PR_NUMBER is required for pull_request events.", file=sys.stderr)
        sys.exit(1)

    print(f"Analysing PR #{PR_NUMBER} in {REPO} using GitHub Models ({MODEL})")
    pr       = get_pr_info()
    pr_title = pr["title"]
    pr_files = get_pr_files()
    print(f"Total changed files: {len(pr_files)}")

    # ── 1. Commit-by-commit summarization ────────────────────────────────────
    print("\n[1/2] Generating PR summary (commit-by-commit)...")
    summary_md = generate_pr_summary(pr_title)

    # ── 2. Per-file code review ───────────────────────────────────────────────
    print("\n[2/2] Reviewing changed code files...")
    review_md, inline_comments = generate_code_review(pr_title, pr_files)

    # ── Combine into one comment ──────────────────────────────────────────────
    full_body = (
        f"# \U0001f916 AI PR Analysis\n\n"
        f"{summary_md}\n\n"
        "---\n\n"
        f"{review_md}"
    )

    print(f"\nPosting combined analysis ({len(inline_comments)} inline comment(s))...")
    post_review(full_body, inline_comments)
    print("Done!")

if __name__ == "__main__":
    main()
