#!/usr/bin/env python3
"""Fail when PR text would close a crf04/statsplus coordination parent issue.

A child pull request links its parent with "Part of crf04/statsplus#N". A
closing keyword pointed at the parent can close it on merge, before the other
children land, because squash merges copy commit messages into the merge commit.
"""

import json
import os
import re
import subprocess
import sys

PARENT_CLOSE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\s+"
    r"(?:https://github\.com/)?crf04/statsplus(?:#|/issues/)(\d+)\b",
    re.IGNORECASE,
)


def parent_closes(text: str) -> list[str]:
    return [match.group(0) for match in PARENT_CLOSE.finditer(text or "")]


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
    found = [
        (where, hit)
        for where, text in pull_request_texts()
        for hit in parent_closes(text)
    ]
    for where, hit in found:
        print(
            f"::error::PR {where} says '{hit}'. Use 'Part of crf04/statsplus#N' for the parent."
        )
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
