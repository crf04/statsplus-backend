"""Target display-stat vocabulary shared verbatim with the frontend."""

import json
from pathlib import Path

from app.errors import InvalidInputError


TARGET_STAT_KEYS = frozenset(
    json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "docs/contracts/target-stat-catalogue.json"
        ).read_text()
    )
)


def validate_stat_preferences(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise InvalidInputError("Stat preferences must be an object or null.")
    columns = value.get("columns")
    if (
        not isinstance(columns, list)
        or not columns
        or any(
            not isinstance(key, str) or key not in TARGET_STAT_KEYS for key in columns
        )
    ):
        raise InvalidInputError(
            "Stat columns must be a non-empty list of catalogue keys."
        )
    graded_by = value.get("graded_by")
    if not isinstance(graded_by, str) or graded_by not in columns:
        raise InvalidInputError("The grading stat must be one of the selected columns.")
    return {"columns": list(dict.fromkeys(columns)), "graded_by": graded_by}
