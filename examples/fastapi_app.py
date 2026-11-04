"""Mount ACP and AG-UI on one FastAPI app.

Run with:

    HRNS_MODEL=openai:gpt-6.1-sol uvicorn examples.fastapi_app:app --reload
"""

from __future__ import annotations

from fastapi import FastAPI

from hrns.app.agent import build_agent
from hrns.app.fastapi import mount_acp, mount_agui

app = FastAPI(title="hrns example")

# One fresh ACP adapter per WebSocket connection.
mount_acp(app, lambda: build_agent(), path="/acp")

# One AG-UI run per POST.
mount_agui(app, build_agent(), path="/agui")
