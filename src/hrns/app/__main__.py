"""Run the Pydantic AI agent over ACP stdio or FastAPI."""

from __future__ import annotations

from typing import Annotated

from cyclopts import App, Parameter
from pydantic_ai.agent import AbstractAgent

from .acp import run_acp_stdio
from .agent import build_agent

model_option = Parameter(help="Pydantic AI model id; defaults to HRNS_MODEL.")
cwd_option = Parameter(help="Workspace root.")

app = App(name="hrns-app", help="Run the Pydantic AI coding agent over ACP.")


@app.command
def acp(
    *,
    model: Annotated[str | None, model_option] = None,
    cwd: Annotated[str, cwd_option] = ".",
) -> None:
    """Serve ACP over stdio."""
    run_acp_stdio(build_agent(model, cwd=cwd))


@app.command
def serve(
    *,
    model: Annotated[str | None, model_option] = None,
    cwd: Annotated[str, cwd_option] = ".",
    host: str = "127.0.0.1",
    port: int = 8000,
) -> None:
    """Serve ACP over WebSocket and FastAPI."""
    import uvicorn

    from .fastapi import create_app

    def agent_factory(_principal: object) -> AbstractAgent[None, str]:
        return build_agent(model, cwd=cwd)

    uvicorn.run(create_app(agent_factory), host=host, port=port)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
