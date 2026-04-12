#!/usr/bin/env python3
"""
AI Code Review Agent
Uses Anthropic Claude to review GitHub Pull Requests.
Posts a summary comment + inline review comments on changed files.
"""

import json
import os
import re
import sys

import anthropic
import requests

# ── Configuration ──────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GH_TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
PR_NUMBER = int(os.environ.get("PR_NUMBER", "0"))
REPO = os.environ.get("REPO", "")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-3-5-sonnet-20241022")

# File extensions to skip (non-code / binary / generated)
SKIP_EXTENSIONS = {
    ".md", ".txt", ".lock", ".sum", ".json", ".yaml", ".yml", ".toml",
    ".xml", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".pdf",
    ".zip", ".tar", ".gz", ".woff", ".woff2", ".ttf", ".eot",
}

GH_API = "https://api.github.com"
GH_HEADERS = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ── GitHub helpers ─────────────────────────────────────────────────────────────

def gh_get(path, params=None):
    r = requests.get(f"{GH_API}{path}", headers=GH_HEADERS, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def get_pr_info():
    return gh_get(f"/repos/{REPO}/pulls/{PR_NUMBER}")


def get_pr_files():
    all_files, page = [], 1
    while True:
        batch = gh_get(f"/repos/{REPO}/pulls/{PR_NUMBER}/files",
                       params={"per_page": 100, "page": page})
        if not batch:
            break
        all_files.extend(batch)
        page += 1
    return all_files


def get_existing_bot_comment():
    comments = gh_get(f"/repos/{REPO}/issues/{PR_NUMBER}/comments",
                      params={"per_page": 100})
    for c in comments:
        if "<!-- ai-code-reviewer -->" in c.get("body", ""):
            return c["id"]
    return None


def post_or_update_comment(body):
    marker = "<!-- ai-code-reviewer -->"
    full_body = f"{marker}\n{body}"
    existing_id = get_existing_bot_comment()
    if existing_id:
        r = requests.patch(f"{GH_API}/repos/{REPO}/issues/comments/{existing_id}",
                           headers=GH_HEADERS, json={"body": full_body}, timeout=15)
    else:
        r = requests.post(f"{GH_API}/repos/{REPO}/issues/{PR_NUMBER}/comments",
                          headers=GH_HEADERS, json={"body": full_body}, timeout=15)
    r.raise_for_status()


def post_review(summary_body, inline_comments):
    marker = "<!-- ai-code-reviewer -->"
    payload = {
        "body": f"{marker}\n{summary_body}",
        "event": "COMMENT",
    }
    if inline_comments:
        payload["comments"] = inline_comments

    r = requests.post(f"{GH_API}/repos/{REPO}/pulls/{PR_NUMBER}/reviews",
                      headers=GH_HEADERS, json=payload, timeout=30)
    if r.status_code not in (200, 201):
        print(f"  Review API returned {r.status_code}; falling back to plain comment.")
        post_or_update_comment(summary_body)
        return
    print(f"  Review posted with {len(inline_comments)} inline comment(s).")


# ── Diff position mapping ──────────────────────────────────────────────────────

def map_file_lines_to_diff_positions(patch: str) -> dict:
    """
    Returns {new_file_line_number -> diff_position} for a unified diff patch.
    diff_position is 1-indexed from the first line of the patch string.
    Only lines on the RIGHT side (new/added/context) are included.
    """
    mapping = {}
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


# ── Claude review ──────────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are a senior software engineer and code reviewer. "
    "You write precise, actionable review comments. "
    "You focus on correctness, security, performance, and maintainability. "
    "You never add unnecessary praise and never comment on style unless it causes bugs."
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
        pr_title=pr_title,
        filename=filename,
        status=status,
        patch=patch[:4000],
        file_section=file_section,
    )
    message = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    text = message.content[0].text.strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {"summary": text, "rating": "needs_work", "inline_comments": []}
    try:
        return json.loads(m.group())
    except json.JSONDecodeError:
        return {"summary": text, "rating": "needs_work", "inline_comments": []}


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    missing = [v for v in ["ANTHROPIC_API_KEY", "GH_TOKEN", "PR_NUMBER", "REPO"]
               if not os.environ.get(v)]
    if missing or PR_NUMBER == 0:
        print(f"ERROR: Missing env vars: {missing}", file=sys.stderr)
        sys.exit(1)

    print(f"Reviewing PR #{PR_NUMBER} in {REPO} using {MODEL}")
    pr = get_pr_info()
    pr_title = pr["title"]
    pr_files = get_pr_files()
    print(f"Total changed files: {len(pr_files)}")

    reviewable = [
        f for f in pr_files
        if f.get("patch")
        and os.path.splitext(f["filename"])[1].lower() not in SKIP_EXTENSIONS
    ]
    print(f"Code files to review: {len(reviewable)}")

    summary_sections = []
    all_inline_comments = []

    for f in reviewable:
        filename = f["filename"]
        patch = f["patch"]
        status = f.get("status", "modified")
        print(f"  -> {filename}")

        file_content = ""
        if f.get("raw_url") and status != "removed":
            try:
                cr = requests.get(f["raw_url"], headers=GH_HEADERS, timeout=10)
                if cr.status_code == 200:
                    file_content = cr.text
            except Exception as e:
                print(f"    (could not fetch content: {e})")

        try:
            review = review_file(filename, patch, file_content, pr_title, status)
        except Exception as e:
            print(f"    Claude error: {e}")
            continue

        rating = review.get("rating", "needs_work")
        icon = {"good": "\u2705", "needs_work": "\u26a0\ufe0f", "critical": "\U0001f534"}.get(rating, "\u2139\ufe0f")
        summary_sections.append(
            f"### {icon} `{filename}`\n{review.get('summary', 'No summary.')}"
        )

        position_map = map_file_lines_to_diff_positions(patch)
        for ic in review.get("inline_comments", []):
            line_num = ic.get("line")
            diff_pos = position_map.get(line_num)
            if diff_pos is None and position_map:
                nearest = min(position_map.keys(), key=lambda ln: abs(ln - (line_num or 0)))
                diff_pos = position_map[nearest]
            if diff_pos is None:
                continue
            sev = ic.get("severity", "suggestion")
            sev_icon = {"critical": "\U0001f534", "warning": "\U0001f7e1", "suggestion": "\U0001f535"}.get(sev, "\u2139\ufe0f")
            all_inline_comments.append({
                "path": filename,
                "position": diff_pos,
                "body": f"{sev_icon} **{sev.title()}**: {ic.get('comment', '')}",
            })

    if not summary_sections:
        summary_body = (
            "# \U0001f916 AI Code Review\n\n"
            "No reviewable code changes found in this PR "
            "(only non-code / config files were changed)."
        )
    else:
        good = sum(1 for s in summary_sections if "\u2705" in s)
        warn = sum(1 for s in summary_sections if "\u26a0" in s)
        crit = sum(1 for s in summary_sections if "\U0001f534" in s)
        summary_body = (
            f"# \U0001f916 AI Code Review\n\n"
            f"> Reviewed **{len(summary_sections)}** file(s) — "
            f"\u2705 {good} good \u00b7 \u26a0\ufe0f {warn} need work \u00b7 \U0001f534 {crit} critical\n\n"
            f"---\n\n"
            + "\n\n".join(summary_sections)
        )

    print(f"Posting review ({len(all_inline_comments)} inline comment(s))...")
    post_review(summary_body, all_inline_comments)
    print("Done!")


if __name__ == "__main__":
    main()
