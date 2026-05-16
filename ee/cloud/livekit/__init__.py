"""LiveKit — voice/video calling for groups.

Provides:
- ``LiveKitService`` — room management + token generation for LiveKit Cloud
- ``CallAgent`` — AI agent that listens to calls, transcribes, and posts meeting notes
- REST router at ``/api/v1/livekit/*``
"""

from __future__ import annotations

from ee.cloud.livekit.router import router

__all__ = ["router"]
