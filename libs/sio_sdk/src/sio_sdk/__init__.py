"""Python SDK for the Spatial Intelligence OS (PRD M22).

    from sio_sdk import SioClient

    async with SioClient() as sio:
        for entity in await sio.entities(limit=10):
            print(entity.label, entity.state.zone_id)

See `docs/SDK.md` for a quickstart that runs.
"""

from .client import (
    DEFAULT_URL,
    CopilotAnswer,
    Session,
    SioApiError,
    SioClient,
    SioError,
    StreamMessage,
    SyncSioClient,
)

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_URL",
    "CopilotAnswer",
    "Session",
    "SioApiError",
    "SioClient",
    "SioError",
    "StreamMessage",
    "SyncSioClient",
    "__version__",
]
