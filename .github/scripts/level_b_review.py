"""
level_b_review.py — Level B: GPT-5.4 full-file code review for PR workflow.

Reads changed files from PR, sends FULL file content + diff to GPT-5.4,
posts review as PR comment. Adapted from deep_review.py for GitHub Actions.

Exit: 0 = ACCEPT, 1 = REJECT
Output: /tmp/level_b_result.md

Author: Claude Code
Date: 2026-04-04
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

# API settings
MODEL = "gpt-5.4"
MAX_TOKENS = 2000
TEMPERATURE = 0.1
API_TIMEOUT = 45
MAX_FILE_CHARS = 200000


def get_changed_files() -> list[str]:
    """Get changed file paths from PR diff."""
    base_sha = os.environ.get("PR_BASE_SHA", "HEAD~1")
    head_sha = os.environ.get("PR_HEAD_SHA", "HEAD")
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=ACMR",
             f"{base_sha}...{head_sha}"],
            capture_output=True, text=True, timeout=10,
        )
        return [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
    except Exception as e:
        print(f"[Level B] Failed to get changed files: {e}", file=sys.stderr)
        return []


def get_diff() -> str:
    """Get the PR diff."""
    base_sha = os.environ.get("PR_BASE_SHA", "HEAD~1")
    head_sha = os.environ.get("PR_HEAD_SHA", "HEAD")
    try:
        result = subprocess.run(
            ["git", "diff", f"{base_sha}...{head_sha}"],
            capture_output=True, text=True, timeout=30,
        )
        return result.stdout
    except Exception as e:
        print(f"[Level B] Failed to get diff: {e}", file=sys.stderr)
        return ""


def read_file_safe(filepath: str) -> str | None:
    """Read full file content, skipping sensitive/binary files."""
    basename = os.path.basename(filepath)
    if ".env" in basename or basename.endswith((".key", ".pem", ".db", ".pyc")):
        return None
    try:
        with open(filepath, encoding="utf-8") as f:
            content = f.read()
        if len(content) > MAX_FILE_CHARS:
            content = content[:MAX_FILE_CHARS] + f"\n[TRUNCATED at {MAX_FILE_CHARS} chars]"
        return content
    except (OSError, UnicodeDecodeError):
        return None


def load_invariants() -> str:
    """Load CODE_INVARIANTS.md from repo root."""
    for path in ["CODE_INVARIANTS.md", "docs/CODE_INVARIANTS.md"]:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                return f.read()
    return "(CODE_INVARIANTS.md not found in repo)"


def strip_env_changes(diff: str) -> str:
    """Remove .env file diffs to avoid sending credentials to API."""
    lines = diff.split("\n")
    filtered = []
    is_env_diff = False
    for line in lines:
        if line.startswith("diff --git") and ".env" in line:
            is_env_diff = True
            filtered.append(f"{line}\n[REDACTED: .env changes stripped]")
            continue
        if line.startswith("diff --git"):
            is_env_diff = False
        if not is_env_diff:
            filtered.append(line)
    return "\n".join(filtered)


_last_api_error: str = ""  # Module-level: last API error for verbose logging


def call_gpt(diff: str, invariants: str, file_contents: dict[str, str]) -> str | None:
    """Send full files + diff to GPT-5.4 for review.

    Returns review text or None on failure. Sets _last_api_error on failure.
    """
    global _last_api_error
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("[Level B] OPENAI_API_KEY not set", file=sys.stderr)
        return None

    files_section = ""
    for fname, content in file_contents.items():
        files_section += f"\n=== FULL FILE: {fname} ===\n{content}\n"

    prompt = (
        "You are reviewing a pull request to a 24/7 trading infrastructure.\n"
        "The FULL FILE(s) are provided for complete context. "
        "The DIFF shows what changed in this PR.\n\n"
        f"MANDATORY INVARIANTS:\n{invariants}\n\n"
        "KNOWN EXCEPTIONS (do NOT flag these):\n"
        "- write_shared_file allowing .py/.sh is INTENTIONAL (AI file exchange, audited via Telegram)\n"
        "- agent_command forwards to Gemma Agent with whitelist + Delete Guard\n"
        "- CORS * on localhost-bound servers behind UFW is accepted (INV-14 exception)\n\n"
        f"{files_section}\n"
        f"=== DIFF (changes in this PR) ===\n{diff}\n\n"
        "Check the ENTIRE file(s) against all invariants, not just the diff.\n"
        "Focus on: resource leaks, threading issues, security, error handling.\n\n"
        "Output format (STRICTLY follow this):\n"
        "CRITICAL: [list or \"none\"]\n"
        "HIGH: [list or \"none\"]\n"
        "VERDICT: ACCEPT / REJECT / ACCEPT_WITH_FIXES\n"
        "SUMMARY: [2 sentences]"
    )

    payload = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_completion_tokens": MAX_TOKENS,
        "temperature": TEMPERATURE,
    }).encode()

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    delay = 5
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(
                "https://api.openai.com/v1/chat/completions",
                data=payload, headers=headers,
            )
            with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
                data = json.loads(resp.read().decode())
                return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            _last_api_error = f"HTTP {e.code}"
            print(f"[Level B] API HTTP {e.code} (attempt {attempt}/3)", file=sys.stderr)
            if e.code in (400, 401, 403):
                break
            if attempt < 3:
                time.sleep(delay)
                delay *= 2
        except Exception as e:
            _last_api_error = str(e)[:120]
            print(f"[Level B] API error (attempt {attempt}/3): {e}", file=sys.stderr)
            if attempt < 3:
                time.sleep(delay)
                delay *= 2

    return None


def parse_verdict(response: str) -> str:
    """Extract VERDICT from review response."""
    for line in response.split("\n"):
        stripped = line.strip().replace("**", "")
        if stripped.startswith("VERDICT:"):
            return stripped.split(":", 1)[1].strip()
    return "UNKNOWN"


def parse_findings(response: str) -> tuple[int, int, list[str]]:
    """Extract finding counts and descriptions from review response.

    Returns (critical_count, high_count, findings_list).
    """
    critical_count = 0
    high_count = 0
    findings: list[str] = []
    for line in response.split("\n"):
        stripped = line.strip().replace("**", "")
        if stripped.startswith("CRITICAL:"):
            text = stripped.split(":", 1)[1].strip().lower()
            if text not in ("none", '"none"', ""):
                inv_refs = re.findall(r"INV-\d+", stripped, re.IGNORECASE)
                critical_count = len(inv_refs) if inv_refs else 1
                findings.append(f"CRITICAL: {stripped.split(':', 1)[1].strip()}")
        elif stripped.startswith("HIGH:"):
            text = stripped.split(":", 1)[1].strip().lower()
            if text not in ("none", '"none"', ""):
                inv_refs = re.findall(r"INV-\d+", stripped, re.IGNORECASE)
                high_count = len(inv_refs) if inv_refs else 1
                findings.append(f"HIGH: {stripped.split(':', 1)[1].strip()}")
    return critical_count, high_count, findings


def write_github_output(verdict: str, critical: int, high: int, findings: list[str]) -> None:
    """Write structured JSON summary to $GITHUB_OUTPUT for consilium."""
    summary = json.dumps({
        "verdict": verdict,
        "critical_count": critical,
        "high_count": high,
        "findings": findings[:10],  # Cap at 10 to avoid output size limits
        "model": MODEL,
        "repo": os.environ.get("GITHUB_REPOSITORY", "unknown"),
    })
    output_file = os.environ.get("GITHUB_OUTPUT", "")
    if output_file:
        with open(output_file, "a", encoding="utf-8") as f:
            f.write(f"review_summary={summary}\n")


def main() -> int:
    """Run Level B GPT-5.4 review on PR files."""
    changed_files = get_changed_files()
    if not changed_files:
        report = "## Level B: GPT-5.4 Full-File Review\n\nNo files changed. **PASS**\n"
        with open("/tmp/level_b_result.md", "w", encoding="utf-8") as f:
            f.write(report)
        return 0

    # Read full content of each changed file
    file_contents: dict[str, str] = {}
    for fpath in changed_files:
        content = read_file_safe(fpath)
        if content is not None:
            file_contents[fpath] = content

    if not file_contents:
        report = "## Level B: GPT-5.4 Full-File Review\n\nNo reviewable files. **PASS**\n"
        with open("/tmp/level_b_result.md", "w", encoding="utf-8") as f:
            f.write(report)
        return 0

    # Get and sanitize diff
    diff = strip_env_changes(get_diff())
    if len(diff) > MAX_FILE_CHARS:
        diff = diff[:MAX_FILE_CHARS] + "\n[TRUNCATED]"

    invariants = load_invariants()

    print(f"[Level B] Reviewing {len(file_contents)} file(s) with {MODEL}...")
    response = call_gpt(diff, invariants, file_contents)

    if response is None:
        error_detail = f" ({_last_api_error})" if _last_api_error else ""
        report = (
            "## Level B: GPT-5.4 Full-File Review\n\n"
            f"**ERROR**: GPT-5.4 API failed after 3 attempts{error_detail}. Review skipped.\n"
        )
        with open("/tmp/level_b_result.md", "w", encoding="utf-8") as f:
            f.write(report)
        write_github_output("UNKNOWN", 0, 0, [f"API failed after 3 attempts{error_detail}"])
        return 1

    verdict = parse_verdict(response)
    critical_count, high_count, findings = parse_findings(response)
    files_list = ", ".join(f"`{f}`" for f in file_contents.keys())

    report = (
        f"## Level B: GPT-5.4 Full-File Review\n\n"
        f"**Model:** {MODEL} | **Files:** {files_list}\n\n"
        f"---\n\n{response}\n"
    )
    with open("/tmp/level_b_result.md", "w", encoding="utf-8") as f:
        f.write(report)

    write_github_output(verdict, critical_count, high_count, findings)
    print(f"[Level B] VERDICT: {verdict} ({critical_count} critical, {high_count} high)")
    if verdict in ("REJECT",):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
