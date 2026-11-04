"""Run the Pydantic AI agent over ACP stdio or FastAPI."""

from __future__ import annotations

import argparse

from pydantic_ai.agent import AbstractAgent

from .acp import run_acp_stdio
from .agent import build_agent


def main() -> None:
    parser = argparse.ArgumentParser(prog="hrns-app")
    sub = parser.add_subparsers(dest="command", required=True)

    acp = sub.add_parser("acp", help="Serve ACP over stdio.")
    acp.add_argument("--model", help="Pydantic AI model id; defaults to HRNS_MODEL.")
    acp.add_argument("--cwd", default=".", help="Workspace root.")

    serve = sub.add_parser("serve", help="Serve ACP over WebSocket and FastAPI.")
    serve.add_argument("--model", help="Pydantic AI model id; defaults to HRNS_MODEL.")
    serve.add_argument("--cwd", default=".", help="Workspace root.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    args = parser.parse_args()

    if args.command == "acp":
        run_acp_stdio(build_agent(args.model, cwd=args.cwd))
        return

    import uvicorn

    from .fastapi import create_app

    def agent_factory(_principal: object) -> AbstractAgent[None, str]:
        return build_agent(args.model, cwd=args.cwd)

    uvicorn.run(create_app(agent_factory), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
