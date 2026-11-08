"""Strict JSON action parsing at the policy boundary."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

#: ``read_forum`` is deliberately absent. The forum is in view on every turn from step 0, so
#: there is nothing to load, and a run that never loaded it used to see no peer count at all.
ALLOWED_ACTIONS = {
    "inspect_repo",
    "run_tests",
    "read_docs",
    "post_forum",
    "run_helper",
    "apply_patch",
    "finish",
}


class ActionParseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Action:
    name: str
    content: str = ""
    patch: str = ""
    summary: str = ""
    admission: bool | None = None
    disclosure: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.name,
            "admission": self.admission,
            "content": self.content,
            "disclosure": self.disclosure,
            "patch": self.patch,
            "summary": self.summary,
        }


def parse_action(value: str | Mapping[str, Any]) -> Action:
    if isinstance(value, str):
        try:
            raw = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ActionParseError(f"invalid JSON: {exc.msg}") from exc
    elif isinstance(value, Mapping):
        raw = dict(value)
    else:
        raise ActionParseError("policy output must be a JSON object or mapping")
    if not isinstance(raw, dict):
        raise ActionParseError("action must be a JSON object")
    unknown = set(raw) - {"action", "content", "patch", "summary", "admission", "disclosure"}
    if unknown:
        raise ActionParseError(f"unknown action fields: {sorted(unknown)}")
    name = raw.get("action")
    if name not in ALLOWED_ACTIONS:
        raise ActionParseError(f"unknown action: {name!r}")
    fields: dict[str, str] = {}
    for key in ("content", "patch", "summary"):
        item = raw.get(key, "")
        if not isinstance(item, str):
            raise ActionParseError(f"{key} must be a string")
        fields[key] = item[:4096]
    admission = raw.get("admission")
    if admission is not None and not isinstance(admission, bool):
        raise ActionParseError("admission must be true, false, or null")
    disclosure = raw.get("disclosure", False)
    if not isinstance(disclosure, bool):
        raise ActionParseError("disclosure must be true or false")
    return Action(name=name, admission=admission, disclosure=disclosure, **fields)
