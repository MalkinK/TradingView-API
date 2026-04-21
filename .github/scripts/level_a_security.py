"""
level_a_security.py — Level A: Lint & security pattern scan for PR review.

Scans only NEWLY ADDED lines in Python files for security anti-patterns.
Does not flag existing code — only new introductions in this PR.

Exit: 0 = PASS, 1 = CRITICAL findings in new code
Output: /tmp/level_a_result.md

Author: Claude Code
Date: 2026-04-04
"""

import os
import re
import subprocess
import sys


# Security patterns: (regex, severity, description)
PATTERNS: list[tuple[str, str, str]] = [
    (r"0\.0\.0\.0", "CRITICAL", "Binding to 0.0.0.0 — exposes service to network"),
    (r"\beval\s*\(", "CRITICAL", "eval() usage — code injection risk"),
    (r"\bexec\s*\((?!_info)", "CRITICAL", "exec() usage — code injection risk"),
    (r"str\(e\).*(?:return|jsonify|response)", "HIGH", "str(e) leaked to client — information disclosure"),
    (r"BEGIN\s+(?:RSA\s+|OPENSSH\s+|EC\s+)?PRIVATE", "CRITICAL", "Private key in source code"),
    (r"<<<<<<< ", "CRITICAL", "Merge conflict marker"),
    (r"password\s*=\s*['\"][^'\"]+['\"]", "HIGH", "Hardcoded password"),
    (r"(?:sk-ant-api|xai-|sk-proj-)[a-zA-Z0-9_-]{10,}", "CRITICAL", "API key in source code"),
]


def get_added_lines_from_diff() -> dict[str, list[str]]:
    """Get only the added lines per Python file from the PR diff.

    Returns {filepath: [added_line_contents]} — only lines starting with '+' in the diff.
    This ensures we only flag NEW security issues, not existing code.
    """
    base_sha = os.environ.get("PR_BASE_SHA", "HEAD~1")
    head_sha = os.environ.get("PR_HEAD_SHA", "HEAD")
    try:
        result = subprocess.run(
            ["git", "diff", f"{base_sha}...{head_sha}", "--", "*.py"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as e:
        print(f"[Level A] Failed to get diff: {e}", file=sys.stderr)
        return {}

    added: dict[str, list[str]] = {}
    current_file: str | None = None

    for line in result.stdout.split("\n"):
        if line.startswith("diff --git"):
            match = re.search(r"b/(.*\.py)", line)
            current_file = match.group(1) if match else None
            if current_file:
                added.setdefault(current_file, [])
        elif line.startswith("+") and not line.startswith("+++"):
            if current_file:
                added[current_file].append(line[1:])  # Strip the leading '+'

    return added


def scan_lines(lines: list[str]) -> list[tuple[str, str, str]]:
    """Scan a list of added lines for security patterns.

    Returns list of (line_content, severity, description).
    """
    findings: list[tuple[str, str, str]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith('"""') or stripped.startswith("'''"):
            continue

        for pattern, severity, description in PATTERNS:
            if re.search(pattern, line):
                findings.append((stripped[:80], severity, description))

    return findings


def main() -> int:
    """Run Level A security scan on added lines only."""
    added_by_file = get_added_lines_from_diff()

    report_lines: list[str] = ["## Level A: Lint & Security Patterns\n"]
    critical_count = 0
    high_count = 0

    if not added_by_file:
        report_lines.append("No Python lines added. **PASS**\n")
        with open("/tmp/level_a_result.md", "w", encoding="utf-8") as f:
            f.write("\n".join(report_lines))
        return 0

    total_lines = sum(len(v) for v in added_by_file.values())
    report_lines.append(f"Scanned {total_lines} new line(s) across {len(added_by_file)} file(s):\n")

    for filepath, lines in added_by_file.items():
        findings = scan_lines(lines)
        if not findings:
            continue

        report_lines.append(f"### `{filepath}`\n")
        for line_content, severity, description in findings:
            icon = "🔴" if severity == "CRITICAL" else "🟡"
            report_lines.append(f"- {icon} **{severity}**: {description}")
            report_lines.append(f"  `{line_content}`")
            if severity == "CRITICAL":
                critical_count += 1
            elif severity == "HIGH":
                high_count += 1
        report_lines.append("")

    # Verdict
    report_lines.append("---\n")
    if critical_count > 0:
        report_lines.append(f"**VERDICT: FAIL** ({critical_count} critical, {high_count} high)")
    elif high_count > 0:
        report_lines.append(f"**VERDICT: PASS WITH WARNINGS** ({high_count} high)")
    else:
        report_lines.append("**VERDICT: PASS**")

    with open("/tmp/level_a_result.md", "w", encoding="utf-8") as f:
        f.write("\n".join(report_lines))

    print(f"[Level A] {critical_count} critical, {high_count} high findings")
    return 1 if critical_count > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
