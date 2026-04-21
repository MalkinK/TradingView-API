"""
level_e_review.py — Level E: Grok 4.20 full-file code review for PR workflow.

Reads changed files from PR, sends FULL file content to Grok 4.20 via xAI API.
Advisory only — continue-on-error in workflow YAML (~40% reliability).

Exit: 0 = ACCEPT, 1 = REJECT (but never blocks merge due to continue-on-error)
Output: /tmp/level_e_result.md

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
MODEL = "grok-4.20-0309-reasoning"
MAX_TOKENS = 2000
API_TIMEOUT = 60
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
        print(f"[Level E] Failed to get changed files: {e}", file=sys.stderr)
        return []


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


_last_api_error: str = ""  # Module-level: last API error for verbose logging


def check_api_health() -> bool:
    """Quick ping to xAI API before full review (15s timeout for Israel latency)."""
    api_key = os.environ.get("XAI_API_KEY", "")
    if not api_key:
        return False
    try:
        req = urllib.request.Request(
            "https://api.x.ai/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"[Level E] API health check failed: {e}", file=sys.stderr)
        return False


def call_grok(file_contents: dict[str, str], invariants: str) -> str | None:
    """Send full files to Grok 4.20 for review.

    Returns review text or None on failure. Sets _last_api_error on failure.
    """
    global _last_api_error
    api_key = os.environ.get("XAI_API_KEY")
    if not api_key:
        print("[Level E] XAI_API_KEY not set", file=sys.stderr)
        return None

    files_section = ""
    for fname, content in file_contents.items():
        files_section += f"\n=== FULL FILE: {fname} ===\n{content}\n"

    prompt = (
        "You are a senior security-focused code reviewer for a 24/7 automated trading infrastructure.\n\n"
        f"MANDATORY INVARIANTS:\n{invariants}\n\n"
        "KNOWN EXCEPTIONS (do NOT flag these):\n"
        "- write_shared_file allowing .py/.sh is INTENTIONAL (AI file exchange, audited via Telegram)\n"
        "- agent_command forwards to Gemma Agent with whitelist + Delete Guard\n"
        "- CORS * on localhost-bound servers behind UFW is accepted (INV-14 exception)\n\n"
        f"{files_section}\n"
        "Output format (STRICTLY):\n"
        "CRITICAL: [list or \"none\"]\n"
        "HIGH: [list or \"none\"]\n"
        "MEDIUM: [list or \"none\"]\n"
        "VERDICT: ACCEPT / REJECT / ACCEPT_WITH_FIXES\n"
        "SUMMARY: [2 sentence assessment]"
    )

    payload = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_completion_tokens": MAX_TOKENS,
    }).encode()

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    }

    delay = 5
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(
                "https://api.x.ai/v1/chat/completions",
                data=payload, headers=headers,
            )
            with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
                data = json.loads(resp.read().decode())
                return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            _last_api_error = f"HTTP {e.code}"
            print(f"[Level E] API HTTP {e.code} (attempt {attempt}/3)", file=sys.stderr)
            if e.code in (400, 401, 403):
                break
            if attempt < 3:
                time.sleep(delay)
                delay *= 2
        except Exception as e:
            _last_api_error = str(e)[:120]
            print(f"[Level E] API error (attempt {attempt}/3): {e}", file=sys.stderr)
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
                critical_count += 1
                findings.append(f"CRITICAL: {stripped.split(':', 1)[1].strip()}")
        elif stripped.startswith("HIGH:"):
            text = stripped.split(":", 1)[1].strip().lower()
            if text not in ("none", '"none"', ""):
                high_count += 1
                findings.append(f"HIGH: {stripped.split(':', 1)[1].strip()}")
    return critical_count, high_count, findings


def write_github_output(verdict: str, critical: int, high: int, findings: list[str]) -> None:
    """Write structured JSON summary to $GITHUB_OUTPUT for consilium."""
    summary = json.dumps({
        "verdict": verdict,
        "critical_count": critical,
        "high_count": high,
        "findings": findings[:10],
        "model": MODEL,
        "repo": os.environ.get("GITHUB_REPOSITORY", "unknown"),
    })
    output_file = os.environ.get("GITHUB_OUTPUT", "")
    if output_file:
        with open(output_file, "a", encoding="utf-8") as f:
            f.write(f"review_summary={summary}\n")


def main() -> int:
    """Run Level E Grok 4.20 review on PR files."""
    if not check_api_health():
        print("[Level E] xAI API unavailable, skipping review", file=sys.stderr)
        report = (
            "## Level E: Grok Full-File Review\n\n"
            "**SKIPPED**: xAI API health check failed. Review not attempted.\n\n"
            "_This check is advisory only and does not block merge._\n"
        )
        with open("/tmp/level_e_result.md", "w", encoding="utf-8") as f:
            f.write(report)
        write_github_output("SKIP", 0, 0, ["API health check failed - xAI unavailable"])
        return 0

    changed_files = get_changed_files()
    if not changed_files:
        report = "## Level E: Grok 4.20 Full-File Review\n\nNo files changed. **PASS**\n"
        with open("/tmp/level_e_result.md", "w", encoding="utf-8") as f:
            f.write(report)
        return 0

    # Read full content of each changed file
    file_contents: dict[str, str] = {}
    for fpath in changed_files:
        content = read_file_safe(fpath)
        if content is not None:
            file_contents[fpath] = content

    if not file_contents:
        report = "## Level E: Grok 4.20 Full-File Review\n\nNo reviewable files. **PASS**\n"
        with open("/tmp/level_e_result.md", "w", encoding="utf-8") as f:
            f.write(report)
        return 0

    invariants = load_invariants()

    print(f"[Level E] Reviewing {len(file_contents)} file(s) with {MODEL}...")
    response = call_grok(file_contents, invariants)

    if response is None:
        error_detail = f" ({_last_api_error})" if _last_api_error else ""
        report = (
            "## Level E: Grok 4.20 Full-File Review\n\n"
            f"**ERROR**: Grok API failed after 3 attempts{error_detail}. Review skipped.\n\n"
            "_This check is advisory only and does not block merge._\n"
        )
        with open("/tmp/level_e_result.md", "w", encoding="utf-8") as f:
            f.write(report)
        write_github_output("UNKNOWN", 0, 0, [f"API failed after 3 attempts{error_detail}"])
        return 1

    verdict = parse_verdict(response)
    critical_count, high_count, findings = parse_findings(response)
    files_list = ", ".join(f"`{f}`" for f in file_contents.keys())

    report = (
        f"## Level E: Grok 4.20 Full-File Review\n\n"
        f"**Model:** {MODEL} | **Files:** {files_list}\n\n"
        f"_This check is advisory only and does not block merge._\n\n"
        f"---\n\n{response}\n"
    )
    with open("/tmp/level_e_result.md", "w", encoding="utf-8") as f:
        f.write(report)

    write_github_output(verdict, critical_count, high_count, findings)
    print(f"[Level E] VERDICT: {verdict} ({critical_count} critical, {high_count} high)")
    if verdict in ("REJECT",):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
