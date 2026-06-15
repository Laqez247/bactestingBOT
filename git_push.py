#!/usr/bin/env python3
"""
git_push.py — Push current workspace changes to GitHub using the GitHub API.

Usage:
    python git_push.py --token YOUR_PAT
    python git_push.py --token YOUR_PAT --message "Backtest iteration 2 results"

The GitHub PAT needs 'repo' scope (or 'public_repo' for public repos).
Set env var GITHUB_TOKEN instead of passing --token each time.
"""

import os, sys, json, base64, argparse, subprocess, hashlib
from pathlib import Path
import urllib.request, urllib.error

REPO_OWNER = "Laqez247"
REPO_NAME  = "bactestingBOT"
BRANCH     = "main"
API_BASE   = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}"

TRACK_DIRS = ["edge2_backtest"]
TRACK_EXTS = {".py", ".txt", ".csv", ".json", ".md", ".toml", ".yaml", ".yml"}
IGNORE_DIRS = {"__pycache__", ".git", "cache", ".mypy_cache", ".pytest_cache"}


def api(token: str, method: str, path: str, body=None):
    url = f"{API_BASE}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github.v3+json")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()
        raise RuntimeError(f"GitHub API {method} {path} → {e.code}: {body_text}") from e


def get_file_sha(token: str, path: str):
    """Return SHA of file on GitHub (None if not found)."""
    try:
        result = api(token, "GET", f"/contents/{path}?ref={BRANCH}")
        return result.get("sha")
    except RuntimeError:
        return None


def collect_files(root: Path, base: Path):
    """Yield (rel_path_str, content_bytes) for all tracked files."""
    for d in TRACK_DIRS:
        target = root / d
        if not target.exists():
            continue
        for fpath in sorted(target.rglob("*")):
            if fpath.is_dir():
                continue
            # Skip ignored dirs
            if any(ig in fpath.parts for ig in IGNORE_DIRS):
                continue
            if fpath.suffix not in TRACK_EXTS:
                continue
            rel = str(fpath.relative_to(base))
            try:
                yield rel, fpath.read_bytes()
            except Exception as e:
                print(f"  [SKIP] {rel}: {e}")


def push(token: str, message: str):
    root = Path(__file__).parent.parent  # workspace root
    files = list(collect_files(root, root))
    print(f"Found {len(files)} files to sync")

    pushed = 0
    skipped = 0
    for rel, content in files:
        b64 = base64.b64encode(content).decode()
        sha = get_file_sha(token, rel)
        body = {
            "message": message,
            "content": b64,
            "branch": BRANCH,
        }
        if sha:
            # Content hash check: skip if identical
            remote_hash = sha  # GitHub SHA is not content hash, always update
            body["sha"] = sha
        try:
            api(token, "PUT", f"/contents/{rel}", body)
            print(f"  ✓ {rel}")
            pushed += 1
        except RuntimeError as e:
            print(f"  ✗ {rel}: {e}")

    print(f"\nDone: {pushed} files pushed, {skipped} skipped")


def main():
    parser = argparse.ArgumentParser(description="Push workspace to GitHub")
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""), help="GitHub PAT")
    parser.add_argument("--message", default="Auto-push: backtest update", help="Commit message")
    args = parser.parse_args()

    if not args.token:
        print("ERROR: Provide --token YOUR_PAT or set GITHUB_TOKEN env var")
        print("  Get a PAT at: https://github.com/settings/tokens")
        print("  Needs: public_repo scope (or repo for private)")
        sys.exit(1)

    print(f"Pushing to {REPO_OWNER}/{REPO_NAME} (branch: {BRANCH})")
    print(f"Message: {args.message}")
    print()
    push(args.token, args.message)


if __name__ == "__main__":
    main()
