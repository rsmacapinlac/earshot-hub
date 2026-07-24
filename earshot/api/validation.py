"""Bind request/response validation to the OpenAPI component schemas.

`openapi.yaml` is the single source of truth. OpenAPI 3.1 component schemas are
JSON Schema (Draft 2020-12), so we validate instances straight against
`#/components/schemas/<Name>` — no parallel schema files to drift.
"""

from __future__ import annotations

import functools
from importlib.resources import files
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

_OPENAPI_URI = "urn:earshot:openapi"


class SchemaValidationError(ValueError):
    """Raised when an instance does not match a named component schema."""

    def __init__(self, schema_name: str, errors: list[str]):
        self.schema_name = schema_name
        self.errors = errors
        super().__init__(
            f"{schema_name}: " + "; ".join(errors) if errors else schema_name
        )


@functools.lru_cache(maxsize=1)
def openapi_document() -> dict[str, Any]:
    """The parsed OpenAPI document."""
    text = files("earshot.api").joinpath("openapi.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(text)


@functools.lru_cache(maxsize=1)
def _registry() -> Registry:
    resource = Resource.from_contents(
        openapi_document(), default_specification=DRAFT202012
    )
    return Registry().with_resource(uri=_OPENAPI_URI, resource=resource)


def component_names() -> list[str]:
    return sorted(openapi_document().get("components", {}).get("schemas", {}))


@functools.lru_cache(maxsize=None)
def validator_for(schema_name: str) -> Draft202012Validator:
    if schema_name not in openapi_document().get("components", {}).get("schemas", {}):
        raise KeyError(f"no component schema named {schema_name!r}")
    schema = {"$ref": f"{_OPENAPI_URI}#/components/schemas/{schema_name}"}
    return Draft202012Validator(schema, registry=_registry())


def error_messages(instance: Any, schema_name: str) -> list[str]:
    """Human-readable validation errors, empty if the instance is valid."""
    validator = validator_for(schema_name)
    messages: list[str] = []
    for err in sorted(validator.iter_errors(instance), key=lambda e: list(e.path)):
        location = "/".join(str(p) for p in err.path)
        messages.append(f"{location or '(root)'}: {err.message}")
    return messages


def validate(instance: Any, schema_name: str) -> None:
    """Raise :class:`SchemaValidationError` if the instance is invalid."""
    messages = error_messages(instance, schema_name)
    if messages:
        raise SchemaValidationError(schema_name, messages)


def is_valid(instance: Any, schema_name: str) -> bool:
    return not error_messages(instance, schema_name)
