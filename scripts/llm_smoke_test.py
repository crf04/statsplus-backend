"""
Manual smoke test script for the live LLM service.

This calls the OpenAI API and is intentionally kept out of pytest collection.
Set OPENAI_API_KEY before running it directly:

    python scripts/llm_smoke_test.py ["custom query" ...]
"""

import os
import sys
import time

# Add the project root to the Python path when run from scripts/.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.services.llm_service import LLMError, LLMService

# This file is a manual live-API smoke script, not a pytest test module.
__test__ = False

DEFAULT_QUERIES = [
    "LeBron James last 10 games",
    "Stephen Curry this season",
    "Kevin Durant vs catch and shoot teams",
    "Luka Doncic with 25+ points",
    "Giannis at home last 5 games",
    "Anthony Davis vs top 5 transition teams",
    "Damian Lillard without CJ McCollum",
    "Jayson Tatum with Jaylen Brown last 15 games",
    "Curry vs top 5 defenses at home",
]


def main() -> int:
    queries = sys.argv[1:] or DEFAULT_QUERIES
    try:
        service = LLMService()
    except LLMError as error:
        print(f"LLM service unavailable: {error}")
        return 1

    print(f"Model: {service.config.model}")
    failures = 0
    for index, query in enumerate(queries, 1):
        if index > 1:
            time.sleep(1)  # Stay well inside rate limits.
        print(f"\n{index}. {query}")
        started = time.perf_counter()
        try:
            parsed = service.parse_query(query)
        except LLMError as error:
            failures += 1
            print(f"   FAILED: {error}")
            continue
        elapsed = time.perf_counter() - started
        print(f"   {elapsed:.2f}s")
        print("   " + parsed.model_dump_json(exclude_none=True))

    print(f"\n{len(queries) - failures}/{len(queries)} parsed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
