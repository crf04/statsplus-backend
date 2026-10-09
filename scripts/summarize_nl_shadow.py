"""Summarize NLP-vs-LLM shadow comparisons from application logs.

Reads log text on stdin and aggregates every ``nl_shadow {...}`` line written by
``app.services.nl_shadow``: how often the confident NLP parse and the LLM
agree, which Filter Set fields they disagree on, and example disagreements.
Lines may be JSON log objects (the message is used) or plain text.
Surrounding log prefixes (timestamps, levels, Railway decoration) are ignored.

    railway logs | python scripts/summarize_nl_shadow.py
    python scripts/summarize_nl_shadow.py --examples 10 < saved.log
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter

MARKER = "nl_shadow {"


def _unwrap_json_envelope(line: str) -> str:
    """Return the ``message`` of a JSON log line, or ``line`` if it is plain text.

    Application logs are JSON objects (``app.utils.json_logging``) while older
    exports are plain text, so both are accepted.
    """
    stripped = line.strip()
    if stripped.startswith("{"):
        try:
            envelope = json.loads(stripped)
        except json.JSONDecodeError:
            return line
        if isinstance(envelope, dict) and isinstance(envelope.get("message"), str):
            return envelope["message"]
    return line


def parse_records(lines):
    """Yield each shadow comparison record found in ``lines``."""
    for line in lines:
        line = _unwrap_json_envelope(line)
        start = line.find(MARKER)
        if start < 0:
            continue
        try:
            yield json.loads(line[start + len("nl_shadow "):])
        except json.JSONDecodeError:
            continue


def summarize(records, examples: int = 5) -> dict:
    """Agreement rate, per-field disagreement counts, and sample disagreements."""
    outcomes = Counter()
    fields = Counter()
    latencies = []
    samples = []
    for record in records:
        outcome = record.get("outcome", "unknown")
        outcomes[outcome] += 1
        if "latency_ms" in record:
            latencies.append(record["latency_ms"])
        if outcome == "disagree":
            fields.update(record.get("differences", {}).keys())
            if len(samples) < examples:
                samples.append(record)
    compared = outcomes["agree"] + outcomes["disagree"]
    latencies.sort()
    return {
        "comparisons": sum(outcomes.values()),
        "outcomes": dict(outcomes),
        # Errors are excluded: an LLM failure says nothing about NLP accuracy.
        "agreement_rate": round(outcomes["agree"] / compared, 4) if compared else None,
        "disagreements_by_field": dict(fields.most_common()),
        "median_latency_ms": latencies[len(latencies) // 2] if latencies else None,
        "examples": samples,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--examples", type=int, default=5, help="disagreements to print")
    args = parser.parse_args(argv)
    print(json.dumps(summarize(parse_records(sys.stdin), args.examples), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
