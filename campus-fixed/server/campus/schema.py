"""Validation against the shared, frozen JSON Schema contracts."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker


def uid() -> str:
    return str(uuid4())


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


@lru_cache(maxsize=1)
def contracts() -> dict:
    path = Path(__file__).resolve().parents[2] / "campus-contracts" / "domain-v1.schema.json"
    return json.loads(path.read_text(encoding="utf-8-sig"))


@lru_cache(maxsize=128)
def validator(schema_name: str) -> Draft202012Validator:
    document = contracts()
    if schema_name not in document["$defs"]:
        raise ValueError(f"Unknown shared schema: {schema_name}")
    return Draft202012Validator(
        {"$ref": f"#/$defs/{schema_name}", "$defs": document["$defs"]},
        format_checker=FormatChecker(),
    )


def validate(schema_name: str, data) -> None:
    from .platform import APIError

    errors = sorted(validator(schema_name).iter_errors(data), key=lambda e: str(list(e.absolute_path)))
    if errors:
        # Do not echo submitted secrets or content in validation error strings.
        details = {
            "schema": schema_name,
            "errors": [{"path": "/".join(map(str, e.absolute_path)), "rule": e.validator} for e in errors[:12]],
        }
        raise APIError(422, "VALIDATION_ERROR", "请求数据不符合接口约定", details)
