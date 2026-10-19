"""Start a credential-free, loopback-only hosted demo in the current environment."""

import hashlib
import os
import sys
from pathlib import Path

import uvicorn
from harness_server import create_app


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    token = os.environ.get("HARNESS_DEMO_TOKEN", "harness-local-demo")
    config = {
        "server": {
            "host": "127.0.0.1",
            "port": 8080,
            "store_path": str(root / ".harness-demo"),
            "require_tls": True,
        },
        "deployments": [{
            "id": "demo.v1",
            "name": "Offline demo",
            "version": "1.0.0",
            "description": "Native Pydantic AI demo with policy-controlled tone; no API key needed.",
            "command": [sys.executable, "-m", "harness_runtime.host", "harness_runtime.examples:demo_definition"],
            "cwd": str(root),
            "allowed_cwds": [str(root)],
            "default_cwd": str(root),
        }],
        "principals": [{
            "id": "demo-user",
            "token_hashes": [hashlib.sha256(token.encode()).hexdigest()],
            "allowed_agent_ids": ["demo.v1"],
        }],
    }
    print("Hosted demo: http://127.0.0.1:8080 · agent demo.v1", flush=True)
    print("Client token: HARNESS_DEMO_TOKEN (default: harness-local-demo)", flush=True)
    print(
        "Connect with: harness profiles add demo --server http://127.0.0.1:8080 "
        "--agent demo.v1 --token env:HARNESS_DEMO_TOKEN",
        flush=True,
    )
    uvicorn.run(create_app(config), host="127.0.0.1", port=8080,
                ws_max_size=16 * 1024 * 1024, ws_ping_interval=30, ws_ping_timeout=30)


if __name__ == "__main__":
    main()
