"""Versioned definition document loading and top-level schema checks.

The immutable domain and resolver live in ``statistic_catalog``; this module
owns the file-format boundary so malformed YAML never reaches application
assembly or provider construction.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml


SUPPORTED_SCHEMA_VERSION = 1


class StatisticDefinitionError(ValueError):
    """A statistic definition document cannot be decoded safely."""


class _UniqueKeyLoader(yaml.SafeLoader):
    """PyYAML loader that does not discard duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.Node, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.YAMLError(f"duplicate definition key {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def load_definition(path: str | Path) -> Mapping[str, Any]:
    """Decode one definition document, preserving duplicate-key failures."""

    definition_path = Path(path)
    try:
        with definition_path.open("r", encoding="utf-8") as stream:
            definition = yaml.load(stream, Loader=_UniqueKeyLoader)
    except FileNotFoundError as error:
        raise StatisticDefinitionError(
            f"statistic catalog definition was not found: {definition_path}"
        ) from error
    except (OSError, yaml.YAMLError, ValueError) as error:
        raise StatisticDefinitionError(
            f"statistic catalog definition could not be loaded: {definition_path}"
        ) from error
    if not isinstance(definition, Mapping):
        raise StatisticDefinitionError("statistic catalog definition must be an object")
    validate_schema_version(definition)
    return definition


def validate_schema_version(definition: Mapping[str, Any]) -> int:
    """Require an explicit ``schema_version`` of exactly the implemented version.

    There is no ``version`` alias: one spelling keeps the document's declared
    version unambiguous.
    """

    if "schema_version" not in definition:
        raise StatisticDefinitionError(
            f"schema_version is required and must be {SUPPORTED_SCHEMA_VERSION}"
        )
    version = definition["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != SUPPORTED_SCHEMA_VERSION:
        raise StatisticDefinitionError(
            f"schema_version {version!r} is not implemented; supported version is {SUPPORTED_SCHEMA_VERSION}"
        )
    return version


__all__ = [
    "SUPPORTED_SCHEMA_VERSION",
    "StatisticDefinitionError",
    "load_definition",
    "validate_schema_version",
]
