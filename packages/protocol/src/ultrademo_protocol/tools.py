"""The agent's tool surface (docs/04 §2).

Tool names match Claude's `^[a-zA-Z0-9_-]{1,128}$` rule, so `operate_click`, never `operate.click`.
Schemas are written by hand in the subset strict tool use accepts: every object sets
`additionalProperties: false`, and there are no numeric ranges or string lengths (those are enforced
here, in the pydantic models, before a tool runs).

The list is frozen and ordered: it sits at the front of the prompt cache, so any change to a name,
description or order re-bills the cached prefix on every live session.
"""

import re
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


class PolicyClass(StrEnum):
    """Decided server-side from the element and arguments, never by the model."""

    ALLOWED = "allowed"
    CONFIRM = "confirm"
    BLOCKED = "blocked"


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ObserveInput(_Input):
    screenshot: bool = False


class NavigateInput(_Input):
    url: str = Field(min_length=1, max_length=2048)


class RefInput(_Input):
    ref: str = Field(pattern=r"^[a-z]\d{1,6}$")


class ClickInput(RefInput):
    pass


class HoverInput(RefInput):
    pass


class TypeInput(RefInput):
    text: str = Field(max_length=2000)
    submit: bool = False


class SelectInput(RefInput):
    value: str = Field(min_length=1, max_length=500)


class PressInput(_Input):
    keys: str = Field(pattern=r"^[A-Za-z0-9+]{1,40}$")


class ScrollInput(_Input):
    dy: int = Field(ge=-5000, le=5000)
    ref: str | None = Field(default=None, pattern=r"^[a-z]\d{1,6}$")


class HighlightInput(RefInput):
    label: str | None = Field(default=None, max_length=120)


class ShowCtaInput(_Input):
    kind: Literal["link", "book", "handoff"]
    label: str = Field(min_length=1, max_length=60)


class HandoffInput(_Input):
    reason: str = Field(min_length=1, max_length=500)


class EndInput(_Input):
    summary: str = Field(min_length=1, max_length=2000)


TOOL_INPUTS: dict[str, type[_Input]] = {
    "operate_observe": ObserveInput,
    "operate_navigate": NavigateInput,
    "operate_click": ClickInput,
    "operate_type": TypeInput,
    "operate_select": SelectInput,
    "operate_press": PressInput,
    "operate_scroll": ScrollInput,
    "operate_hover": HoverInput,
    "operate_highlight": HighlightInput,
    "ui_show_cta": ShowCtaInput,
    "session_handoff": HandoffInput,
    "session_end": EndInput,
}

# Tools the operator executes; the rest are handled by the session agent itself.
OPERATOR_TOOLS = frozenset(n for n in TOOL_INPUTS if n.startswith("operate_"))

_REF = {"type": "string", "description": "Element ref from the latest operate_observe, e.g. e12."}

_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "operate_observe",
        "description": (
            "Read the current screen of the product: URL, title and an accessibility snapshot where "
            "each interactive element has a ref like e12. Call it before acting on a screen you have "
            "not seen, and after an action if you need to confirm what changed. Set screenshot to "
            "true only when the layout or a chart matters, because screenshots are slow and costly."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"screenshot": {"type": "boolean"}},
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "operate_navigate",
        "description": (
            "Open a URL inside the product. Only the product's allowed domains work; anything else "
            "is refused. Prefer clicking through the UI when showing a flow, since viewers learn the "
            "path; navigate directly only to recover or to skip setup."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "Absolute URL or a path."}},
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "operate_click",
        "description": (
            "Click an element. The viewer sees the cursor move to it first. Actions that change data "
            "(save, delete, send, pay) may need the viewer's confirmation; if the result says "
            "needs_confirmation, ask the viewer and wait."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"ref": _REF},
            "required": ["ref"],
            "additionalProperties": False,
        },
    },
    {
        "name": "operate_type",
        "description": (
            "Type text into a field, replacing what is there. Set submit to true to press Enter "
            "afterwards. Use realistic demo values, never the viewer's personal data unless they "
            "asked you to use it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"ref": _REF, "text": {"type": "string"}, "submit": {"type": "boolean"}},
            "required": ["ref", "text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "operate_select",
        "description": "Choose an option in a select or combobox by its visible label.",
        "input_schema": {
            "type": "object",
            "properties": {"ref": _REF, "value": {"type": "string"}},
            "required": ["ref", "value"],
            "additionalProperties": False,
        },
    },
    {
        "name": "operate_press",
        "description": "Press a key or chord on the focused element, e.g. Enter, Escape, Control+K.",
        "input_schema": {
            "type": "object",
            "properties": {"keys": {"type": "string"}},
            "required": ["keys"],
            "additionalProperties": False,
        },
    },
    {
        "name": "operate_scroll",
        "description": (
            "Scroll the page, or the element with the given ref, by dy CSS pixels (positive is down)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"dy": {"type": "integer"}, "ref": _REF},
            "required": ["dy"],
            "additionalProperties": False,
        },
    },
    {
        "name": "operate_hover",
        "description": "Hover over an element to reveal a tooltip or menu.",
        "input_schema": {
            "type": "object",
            "properties": {"ref": _REF},
            "required": ["ref"],
            "additionalProperties": False,
        },
    },
    {
        "name": "operate_highlight",
        "description": (
            "Draw a highlight around an element on the viewer's screen while you explain it. It "
            "does not click anything."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"ref": _REF, "label": {"type": "string"}},
            "required": ["ref"],
            "additionalProperties": False,
        },
    },
    {
        "name": "ui_show_cta",
        "description": (
            "Show one of this demo's configured call-to-action buttons to the viewer, such as "
            "booking a meeting. Use it when the viewer shows buying intent or asks for next steps."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["link", "book", "handoff"]},
                "label": {"type": "string"},
            },
            "required": ["kind", "label"],
            "additionalProperties": False,
        },
    },
    {
        "name": "session_handoff",
        "description": (
            "Ask for a human from the sales team to join, when the viewer requests a person or asks "
            "something you must not answer (custom pricing, contracts, legal)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "session_end",
        "description": "End the session after saying goodbye, with a short summary for the sales team.",
        "input_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
            "additionalProperties": False,
        },
    },
]


def claude_tool_definitions() -> list[dict[str, Any]]:
    """Tool definitions for the Messages API, in a stable order, with strict schemas.

    `eager_input_streaming` is deliberately off: every input here is a few dozen tokens, so the
    server-side buffering it disables costs nothing, and keeping it on keeps strict validation.
    """
    return [{**d, "strict": True} for d in _DEFINITIONS]


class ToolInputError(ValueError):
    pass


def parse_tool_input(name: str, raw: Any) -> _Input:
    model = TOOL_INPUTS.get(name)
    if model is None:
        raise ToolInputError(f"unknown tool {name!r}")
    try:
        return model.model_validate(raw)
    except ValidationError as e:
        raise ToolInputError(e.json(include_url=False)) from e


class ToolResult(BaseModel):
    """What the operator returns for every operate_* call."""

    status: Literal["ok", "needs_confirmation", "blocked", "error"]
    summary: str
    url: str | None = None
    title: str | None = None
    snapshot: str | None = None
    screenshot_png_b64: str | None = None
    element: dict[str, Any] | None = None
    policy: PolicyClass = PolicyClass.ALLOWED
    latency_ms: int = 0
