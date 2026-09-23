# ADR 14: Decisions made while building the first slice (2026-09-22)

Status: accepted for the first slice. Revisit each one at the point noted.

## 1. The operator API is HTTP/JSON, not gRPC
The blueprint (docs/05 §6) listed `grpcio` for agent-to-operator calls. Every call is one small request per model tool call, and it runs in-cluster, so the added latency is negligible next to the browser action itself. HTTP/JSON reuses the pydantic models in `packages/protocol`, needs no codegen, and is easy to curl while debugging.
Revisit when an operator tool starts streaming sub-actions (`operate_run_workflow`).

## 2. The Claude brain owns generation inside LiveKit's `llm_node`
LiveKit's function-tool layer would hide the request shape we depend on for cost: cache breakpoints, context editing, fallbacks and per-message effort. So we override `Agent.llm_node`, which LiveKit documents for this purpose, and run our own tool loop. `AgentSession` gets a placeholder `llm.LLM` only because the pipeline skips generation when `llm` is unset (`agent_activity.py`, livekit-agents 1.8.2).

## 3. Overlays are drawn in the page for now (superseded by ADR 15 §1)
ADR 4 prefers client-drawn overlays. The player doesn't exist yet, so the operator injects a cursor and highlight into a closed shadow root with `pointer-events: none`, and they are visible in the streamed video. Results also carry the element's bounding box, so the player can take over. Drawing in-page can be switched off per sandbox (`draw_overlays`).
Revisit when `apps/web` ships.

## 4. One Chromium per sandbox by default
This isolates tenants at the process level (docs/06 §2). `share_browser=true` switches to separate contexts in one browser, to save memory in development.
Revisit after the ADR 11 sandbox-runtime spike.

## 5. No AWS/EKS, Temporal or Redis in the first slice
The product loop needs none of them yet. The outbox table records events transactionally, so webhooks and workflows can be added without changing any write path.
Revisit at the first design partner with real traffic.

## 6. Session validity is a heuristic for now
A session is `valid` once the viewer has spoken at least twice. The insights pipeline will replace this with a model judgement later, off the hot path.

## 7. Verified against installed sources and docs (not assumed)
- Playwright 1.63: `locator.aria_snapshot(mode="ai", boxes=True)`, plus the `aria-ref=` selector. Refs can be frame-prefixed (`f1e5`).
- LiveKit Agents 1.8.2:
  - `AgentServer` / `@server.rtc_session(agent_name=...)`.
  - `TurnHandlingOptions(turn_detection=inference.TurnDetector(), preemptive_generation={"enabled": False})`. `TurnDetector` falls back to the local `v1-mini` model when it isn't on LiveKit Cloud.
  - `FlushSentinel` flushes TTS.
  - Run with `python -m livekit.agents start <file>`, because `cli.run_app` is the deprecated legacy CLI.
- LiveKit rtc 1.1.18: `VideoSource(w, h, is_screencast=True)`. `RoomConfiguration(agents=[RoomAgentDispatch(...)])` does token dispatch.
- Anthropic Python SDK 1.8.0 (`client.beta.messages.stream`):
  - It accepts `fallbacks="default"`, `context_management`, top-level `cache_control` and `output_config`.
  - Strict tool schemas avoid numeric and length constraints, which the docs list as unsupported.
