"""Action policy (docs/06 §4 "Agent guardrails"): decided here, from the element and arguments.

The model never decides whether an action is safe. Every operate_* call on an element is
classified before it runs:

- blocked: never executed (billing, exporting personal data, credentials, account deletion).
- confirm: executed only after the viewer approves on screen (anything that changes or sends data).
- allowed: everything else.

Per-agent overrides come from `agent_versions.policy` as case-insensitive regex lists, checked in
the order blocked, confirm, allowed, so an override can never loosen a built-in block.
"""

import re
from dataclasses import dataclass, field

from ultrademo_protocol import PolicyClass

_BUILTIN_BLOCKED = [
    r"\b(billing|payment method|credit card|invoice settings)\b",
    r"\b(export|download) (all )?(contacts|users|customers|personal data|data)\b",
    r"\b(api key|access token|secret|password)s?\b",
    r"\bdelete (my |the )?(account|workspace|organi[sz]ation)\b",
]
_BUILTIN_CONFIRM = [
    r"\b(delete|remove|archive|discard|revoke|deactivate|cancel subscription)\b",
    r"\b(send|publish|invite|share|submit|pay|purchase|buy|checkout|transfer)\b",
    r"\b(save|update|apply|confirm|approve|reject)\b",
]


def _compile(patterns: list[str]) -> list[re.Pattern[str]]:
    return [re.compile(p, re.IGNORECASE) for p in patterns]


@dataclass
class Policy:
    blocked: list[re.Pattern[str]] = field(default_factory=lambda: _compile(_BUILTIN_BLOCKED))
    confirm: list[re.Pattern[str]] = field(default_factory=lambda: _compile(_BUILTIN_CONFIRM))
    allowed: list[re.Pattern[str]] = field(default_factory=list)

    @classmethod
    def from_agent(cls, overrides: dict | None) -> "Policy":
        p = cls()
        overrides = overrides or {}
        p.blocked += _compile(overrides.get("blocked", []))
        p.confirm += _compile(overrides.get("confirm", []))
        p.allowed = _compile(overrides.get("allowed", []))
        return p

    def classify(self, tool: str, role: str, name: str, *, submits: bool = False) -> PolicyClass:
        """Classify an action on an element with the given ARIA role and accessible name.

        `submits` is true for a click on a button/link/menu item, a typed value followed by Enter,
        or a key press while a button is focused: the cases that can change or send data.
        """
        if tool in ("operate_hover", "operate_highlight", "operate_scroll", "operate_observe"):
            return PolicyClass.ALLOWED
        if any(p.search(name) for p in self.blocked):
            return PolicyClass.BLOCKED
        if tool == "operate_click" and role in ("button", "menuitem", "link", "menuitemcheckbox", ""):
            submits = True
        if submits and any(p.search(name) for p in self.confirm):
            if any(p.search(name) for p in self.allowed):
                return PolicyClass.ALLOWED
            return PolicyClass.CONFIRM
        return PolicyClass.ALLOWED
