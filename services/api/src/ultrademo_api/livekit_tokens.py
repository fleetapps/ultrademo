"""Viewer tokens with explicit agent dispatch (docs/04 §1 "Dispatch").

The agent is dispatched through `RoomConfiguration.agents` in the viewer's token, which LiveKit
applies when the room is first created; each session gets a fresh room, so that is always the case.
The token is readable by the viewer, so dispatch metadata carries only the session id: the agent
fetches everything else from the api with its internal credential.
"""

import json
from datetime import timedelta

from livekit import api

from ultrademo_api.settings import Settings


def viewer_token(settings: Settings, *, room: str, identity: str, session_id: str) -> str:
    grants = api.VideoGrants(
        room_join=True,
        room=room,
        can_publish=True,
        can_publish_sources=["microphone"],
        can_subscribe=True,
        can_publish_data=True,
        can_update_own_metadata=False,
    )
    config = api.RoomConfiguration(
        empty_timeout=60,
        departure_timeout=30,
        agents=[
            api.RoomAgentDispatch(
                agent_name=settings.agent_name, metadata=json.dumps({"session_id": session_id})
            )
        ],
    )
    return (
        api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret.get_secret_value())
        .with_identity(identity)
        .with_name("Viewer")
        .with_kind("standard")
        .with_grants(grants)
        .with_room_config(config)
        .with_ttl(timedelta(seconds=settings.viewer_token_ttl_s))
        .to_jwt()
    )
