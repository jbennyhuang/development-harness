"""Validated recursive JSON values at SDK, HTTP and durable journal boundaries."""
from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from typing import TypeGuard

type JSONValue = None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]
type JSONObject = dict[str, JSONValue]


def _mapping(value: object) -> TypeGuard[Mapping[object, object]]:
    return isinstance(value, Mapping)


def _sequence(value: object) -> TypeGuard[Sequence[object]]:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def json_value(value: object) -> JSONValue:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("JSON numbers must be finite")
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if _mapping(value):
        items: Mapping[object, object] = value
        result: JSONObject = {}
        for key, item in items.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            result[key] = json_value(item)
        return result
    if _sequence(value):
        sequence: Sequence[object] = value
        return [json_value(item) for item in sequence]
    raise ValueError("Expected a JSON value")


def json_object(value: object) -> JSONObject:
    result = json_value(value)
    if not isinstance(result, dict):
        raise ValueError("Expected a JSON object")
    return result


def decode(text: str) -> JSONValue:
    return json_value(json.loads(text))


def decode_object(text: str) -> JSONObject:
    return json_object(json.loads(text))


def string(value: JSONValue) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected a string")
    return value


def integer(value: JSONValue) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("Expected an integer")
    return value


def number(value: JSONValue) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError("Expected a number")
    return value


def array(value: JSONValue) -> list[JSONValue]:
    if not isinstance(value, list):
        raise ValueError("Expected an array")
    return value


def objects(value: JSONValue) -> list[JSONObject]:
    return [json_object(item) for item in array(value)]


def strings(value: JSONValue) -> list[str]:
    return [string(item) for item in array(value)]


def optional_string(value: JSONValue) -> str | None:
    return None if value is None else string(value)


def boolean(value: JSONValue) -> bool:
    if not isinstance(value, bool):
        raise ValueError("Expected a boolean")
    return value


def optional_integer(value: JSONValue) -> int | None:
    return None if value is None else integer(value)


def optional_object(value: JSONValue) -> JSONObject | None:
    return None if value is None else json_object(value)
