"""Embed an agent with a custom capability in an existing FastAPI application.

Run from the checkout (requires OPENAI_API_KEY, or set EXAMPLE_MODEL=test)::

    uv run uvicorn examples.fastapi_app:app --host 127.0.0.1 --port 8080 --ws-max-size 16777216
    uv run hrns connect --url http://127.0.0.1:8080 --agent example.house_style
"""

import os

from fastapi import FastAPI
from pydantic_ai.capabilities import AbstractCapability

from harness.runtime import AgentDefinition, SQLiteSessionStore
from harness.server import AgentServer


class HouseStyle(AbstractCapability[None]):
    id = "example.house_style"
    description = "Adds concise writing instructions."

    def get_instructions(self) -> str:
        return "Keep answers concise and use plain language."


app = FastAPI()
agents = AgentServer(store=SQLiteSessionStore("./sessions.sqlite3"))
agents.add_agent(
    AgentDefinition(
        agent_id="example.house_style",
        name="House style agent",
        definition_version="1.0.0",
        model=os.environ.get("EXAMPLE_MODEL", "openai:gpt-5-mini"),
        fixed_capabilities=[HouseStyle()],
    )
)
agents.mount(app)
