"""Pre-commit guard: block commits that contain secrets.

Checks every staged file (its staged content, not the working copy) for
secret-looking filenames and values. Run by .githooks/pre-commit.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import PurePosixPath

BLOCKED_NAMES = re.compile(r"(^|/)(\.env(\..*)?|.*\.env|.*\.pem|.*\.key|.*\.p12|token.*\.json|.*\.token)$")
ALLOWED_NAMES = re.compile(r"(^|/)\.env\.example$")

PATTERNS = [
    ("credential assignment",
     re.compile(r"(?i)\b\w*(token|secret|api_?key|password|passwd|mpin|totp)\w*\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-./+]{12,}")),
    ("JWT", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]


def staged_files() -> list[str]:
    out = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"],
                         capture_output=True, text=True, check=True).stdout
    return [f for f in out.split("\0") if f]


def staged_content(path: str) -> str:
    result = subprocess.run(["git", "show", f":{path}"], capture_output=True)
    return result.stdout.decode("utf-8", errors="ignore")


def main() -> int:
    problems = []
    for path in staged_files():
        posix = str(PurePosixPath(path))
        if BLOCKED_NAMES.search(posix) and not ALLOWED_NAMES.search(posix):
            problems.append(f"{path}: file type must never be committed")
            continue
        for lineno, line in enumerate(staged_content(path).splitlines(), 1):
            for label, pattern in PATTERNS:
                if pattern.search(line):
                    problems.append(f"{path}:{lineno}: possible {label}")

    if problems:
        print("COMMIT BLOCKED -- possible secrets found:", file=sys.stderr)
        for p in problems:
            print("  " + p, file=sys.stderr)
        print("Move secrets to the git-ignored .env file. If this is a false positive, "
              "rename/rephrase it rather than bypassing the hook.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
