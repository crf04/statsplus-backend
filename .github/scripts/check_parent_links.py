#!/usr/bin/env python3
"""Check how a child pull request links its issues.

A child pull request links its parent with "Part of crf04/statsplus#N" and
closes its own issue with "Closes #M". Two mistakes fail the check:

- A closing keyword pointed at the parent can close it on merge, before the
  other children land, because squash merges copy commit messages into the
  merge commit.
- A pull request that names a parent but no issue in this repository leaves
  its child issue open after merge. Use "Closes #M" for each issue it finishes,
  or "Part of #M" when it is one of several pull requests for that issue.
"""

import json
import os
import re
import subprocess
import sys

CLOSING = r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\s+"
ISSUE_URL = r"(?:https://github\.com/)?"

PARENT_CLOSE = re.compile(
    CLOSING + ISSUE_URL + r"crf04/statsplus(?:#|/issues/)(\d+)\b",
    re.IGNORECASE,
)
PARENT_LINK = re.compile(
    r"\bpart of:?\s+" + ISSUE_URL + r"crf04/statsplus(?:#|/issues/)\d+\b",
    re.IGNORECASE,
)


def parent_closes(text: str) -> list[str]:
    return [match.group(0) for match in PARENT_CLOSE.finditer(text or "")]


def links_own_issue(text: str, repo: str) -> bool:
    """True when the text closes, or is part of, an issue in `repo`."""
    own = r"(?:#|" + ISSUE_URL + re.escape(repo) + r"(?:#|/issues/))\d+\b"
    keyword = r"(?:" + CLOSING + r"|\bpart of:?\s+)"
    return re.search(keyword + own, text or "", re.IGNORECASE) is not None


def pull_request_texts() -> list[tuple[str, str]]:
    event = json.load(open(os.environ["GITHUB_EVENT_PATH"]))
    pull = event["pull_request"]
    commits = subprocess.run(
        [
            "gh",
            "api",
            "--paginate",
            f"{pull['url']}/commits",
            "--jq",
            ".[].commit.message",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [
        ("title", pull["title"]),
        ("body", pull.get("body") or ""),
        ("commits", commits),
    ]


def main() -> int:
    texts = pull_request_texts()
    failed = False
    for where, text in texts:
        for hit in parent_closes(text):
            failed = True
            print(
                f"::error::PR {where} says '{hit}'. Use 'Part of crf04/statsplus#N' for the parent."
            )
    everything = "\n".join(text for _, text in texts)
    repo = os.environ["GITHUB_REPOSITORY"]
    if PARENT_LINK.search(everything) and not links_own_issue(everything, repo):
        failed = True
        print(
            "::error::PR names a crf04/statsplus parent but no issue in this repository. "
            "Add 'Closes #M' for each child issue it finishes (one keyword per issue), "
            "or 'Part of #M' if more pull requests follow."
        )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
