"""Strict, bounded structured output for host applications.

The legacy CLI validator is deliberately unchanged. This contract is opt-in and
uses a documented subset of JSON Schema 2020-12 without remote references.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any


class OutputSchemaError(ValueError):
    """Invalid or unsupported output schema; raised before model execution."""


_KEYWORDS = frozenset({
    "$schema", "title", "description", "default", "examples", "type", "enum",
    "const", "properties", "required", "additionalProperties", "items",
    "minItems", "maxItems", "uniqueItems", "minProperties", "maxProperties",
    "minLength", "maxLength", "pattern", "minimum", "maximum",
    "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "allOf", "anyOf",
    "oneOf", "not",
})


def validator_for(schema: dict[str, Any]):
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError

    if not isinstance(schema, dict):
        raise OutputSchemaError("The root schema must be an object")

    def check(node: Any) -> None:
        if isinstance(node, bool):
            return
        if not isinstance(node, dict):
            raise OutputSchemaError("Schema nodes must be objects or booleans")
        unknown = node.keys() - _KEYWORDS
        if unknown:
            raise OutputSchemaError("Unsupported schema keywords: " + ", ".join(sorted(unknown)))
        if "$schema" in node and node["$schema"] != "https://json-schema.org/draft/2020-12/schema":
            raise OutputSchemaError("Only JSON Schema 2020-12 is supported")
        for child in node.get("properties", {}).values():
            check(child)
        for key in ("items", "additionalProperties", "not"):
            if key in node:
                check(node[key])
        for key in ("allOf", "anyOf", "oneOf"):
            for child in node.get(key, []):
                check(child)

    try:
        json.dumps(schema, allow_nan=False)
        Draft202012Validator.check_schema(schema)
        check(schema)
    except (SchemaError, TypeError, ValueError) as exc:
        if isinstance(exc, OutputSchemaError):
            raise
        raise OutputSchemaError("Invalid JSON Schema") from exc
    return Draft202012Validator(copy.deepcopy(schema))


@dataclass
class OutputContract:
    schema: dict[str, Any]
    max_retries: int = 2
    submitted: bool = False
    failures: int = 0
    repairs: int = 0
    errors: tuple[str, ...] = ()
    validator: Any = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if type(self.max_retries) is not int or self.max_retries < 0:
            raise OutputSchemaError("max_retries must be a nonnegative integer")
        self.schema = copy.deepcopy(self.schema)
        self.validator = validator_for(self.schema)

    def accept(self, value: Any) -> bool:
        try:
            json.dumps(value, allow_nan=False)
        except (ValueError, TypeError):
            self.errors = ("Output must be a JSON value",)
        else:
            # Never echo rejected values: validation errors may contain secrets.
            self.errors = tuple(
                f"/{'/'.join(map(str, error.absolute_path))}: {error.validator} constraint failed"
                for error in list(self.validator.iter_errors(value))[:5]
            )
        self.submitted = not self.errors
        if self.errors:
            self.failures += 1
        return self.submitted

    def retry(self) -> bool:
        if self.repairs >= self.max_retries:
            return False
        self.repairs += 1
        return True
