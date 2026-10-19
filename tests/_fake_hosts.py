"""Fake ``harness-host`` worker used by the server tests.

The real harness-host belongs to another distribution; the server only needs a
stdio JSON-RPC peer.  This script implements enough behaviour to exercise the
proxy: request/response, notifications in both directions, environment
reporting, deliberate crashes, protocol violations and stubborn shutdown.

Run it directly with ``python _fake_hosts.py --mode echo``; it is also
importable from tests for its path and mode helpers.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

#: Environment keys reported back by ``_fake/context``.
CONTEXT_KEYS = (
    "HARNESS_PRINCIPAL",
    "HARNESS_DEPLOYMENT_ID",
    "HARNESS_ALLOWED_CWDS",
    "HARNESS_STORE",
    "HARNESS_HOSTED",
    "FAKE_RESOURCE",
    "FAKE_MODEL_KEY",
    "PATH",
)

DEFAULT_MODE = "echo"


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _handle_request(state: dict, request: dict) -> None:
    method = str(request.get("method", ""))
    request_id = request.get("id")
    params = request.get("params") or {}

    if method == "_fake/context":
        env = {key: os.environ.get(key) for key in CONTEXT_KEYS}
        harness_env = {
            key: value
            for key, value in os.environ.items()
            if key.startswith("HARNESS_")
        }
        _emit(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "env": env,
                    "harnessEnv": harness_env,
                    "argv": sys.argv,
                    "cwd": os.getcwd(),
                    "pid": os.getpid(),
                    "request": request,
                },
            }
        )
        return

    if method == "_fake/diagnostics":
        _emit(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "notifications": state["notifications"],
                    "requests": state["requests"],
                    "contract": {
                        key: (key in os.environ) for key in CONTEXT_KEYS
                    },
                },
            }
        )
        return

    if method == "_fake/push":
        _emit(
            {
                "jsonrpc": "2.0",
                "method": "_fake/notification",
                "params": {"marker": params.get("marker", "pushed")},
            }
        )
        _emit({"jsonrpc": "2.0", "id": request_id, "result": {"pushed": True}})
        return

    if method == "initialize":
        _emit(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": 1,
                    "agentCapabilities": {},
                    "authMethods": [],
                    "fakeHost": True,
                },
            }
        )
        return

    _emit(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"echo": params, "method": method},
        }
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", default=DEFAULT_MODE)
    parser.add_argument("--pidfile")
    parser.add_argument("--child-pidfile")
    parser.add_argument("--flood-count", type=int, default=8)
    parser.add_argument("--noise-stderr", action="store_true")
    args = parser.parse_args(argv)

    if args.pidfile:
        with open(args.pidfile, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))

    if args.noise_stderr:
        # Includes a token-shaped string to prove stderr is never exposed.
        sys.stderr.write("diagnostic: token=SUPER-SECRET-TOKEN-DO-NOT-FORWARD\n")
        sys.stderr.flush()

    if args.mode == "crash-on-start":
        os._exit(9)
    if args.mode == "bad-line":
        sys.stdout.write("this is not JSON-RPC\n")
        sys.stdout.flush()
    if args.mode == "oversize":
        sys.stdout.write("x" * (17 * 1024 * 1024) + "\n")
        sys.stdout.flush()
        os._exit(0)
    if args.mode == "flood":
        for index in range(args.flood_count):
            _emit({"jsonrpc": "2.0", "method": "_fake/flood", "params": {"i": index}})

    child = None
    if args.mode == "child":
        child = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--mode", "child-sleeper"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if args.child_pidfile:
            with open(args.child_pidfile, "w", encoding="utf-8") as handle:
                handle.write(str(child.pid))

    state = {"notifications": [], "requests": 0, "responded": 0}
    stdin = sys.stdin.buffer
    while True:
        try:
            raw = stdin.readline()
        except Exception:  # pragma: no cover - pipe teardown
            break
        if not raw:
            break
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            # Not our job to police the server here; record and continue.
            state["notifications"].append({"_unparsed": line.decode("utf-8", "replace")})
            continue
        if not isinstance(message, dict):
            continue
        if "id" not in message:
            state["notifications"].append(message)
            continue
        state["requests"] += 1
        _handle_request(state, message)
        state["responded"] += 1
        if args.mode == "crash-after-first-response":
            if state["responded"] == 1:
                os._exit(17)

    if args.mode == "stay":
        while True:
            time.sleep(3600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
