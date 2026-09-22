"""Wire contracts shared by api, operator and session-agent.

One source of truth for the realtime envelope (docs/04 §1), the agent tool surface (docs/04 §2)
and policy classes, so the three services cannot drift apart.
"""

from ultrademo_protocol.envelope import (
    ATTR_AGENT_STATE,
    ATTR_SCREEN_META,
    RPC_CONFIRM,
    RPC_POINTER,
    TOPIC_EVENTS,
    TOPIC_TRANSCRIPT,
    AgentState,
    Envelope,
)
from ultrademo_protocol.tools import (
    TOOL_INPUTS,
    TOOL_NAME_RE,
    PolicyClass,
    ToolResult,
    claude_tool_definitions,
    parse_tool_input,
)

__all__ = [
    "ATTR_AGENT_STATE",
    "ATTR_SCREEN_META",
    "RPC_CONFIRM",
    "RPC_POINTER",
    "TOOL_INPUTS",
    "TOOL_NAME_RE",
    "TOPIC_EVENTS",
    "TOPIC_TRANSCRIPT",
    "AgentState",
    "Envelope",
    "PolicyClass",
    "ToolResult",
    "claude_tool_definitions",
    "parse_tool_input",
]
