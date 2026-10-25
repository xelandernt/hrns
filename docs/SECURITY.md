# Security boundaries

## Identity and scope

Network requests and WebSocket handshakes are authenticated by the application's FastAPI
dependency, which returns a `Principal`. The default principal is `local`. Every stored
session is scoped to `(principal, deployment_id)` where the deployment is derived from the
registered agent id and definition version. Every store read, write, list, and delete
includes that scope; a session id from another scope is reported as unavailable and cannot be
overwritten.

Workspace roots and model allow-lists come from Python registration or the trusted hosted
environment. ACP `cwd`, `_meta`, and client capabilities never expand the server's filesystem
scope. `additionalDirectories` and client-supplied MCP servers are rejected.

## Execution

Local stdio agents and embedded agents execute installed Python code in their own process. A
local workspace is not a sandbox: approved function tools run with the process user's
permissions. Isolation requires an explicitly installed and bound upstream workspace backend;
an unavailable backend fails rather than falling back to local execution.

The client has no remote code loading. Renderers and extension data are data-only. Attachment
content is uploaded as ACP resources; a path is display metadata, not an implicit filesystem
grant.

## Transport

The network subprotocol `harness.acp.v1` carries one JSON-RPC object per UTF-8 text frame.
Frames are limited to 16 MiB; duplicate keys, non-finite numbers, batches, and binary frames
are rejected. Outbound writes have a timeout. Connections are limited to 32 per server and
four per principal.

Both the HTTP catalog and WebSocket transport refuse plaintext remote URLs: catalog requests
require `https://` and agent connections require `wss://`. Plain `http://`/`ws://` is allowed
only for loopback development, so a bearer token is not sent over an unencrypted network by
default. TLS termination and proxy trust belong to the application runner.

## Persistence

The store holds native messages, the ACP transcript, selected bindings, model, cwd, effort,
title, and usage. It does not hold provider credentials, live capability objects, or client
connections. Database credentials belong to the deployment.

Durability follows the upstream contract: save failures are logged and the next save catches
up; there is no transactional turn commit or cross-connection ownership lock. Do not treat a
completed prompt as an exactly-once external effect. Uncertain prompts are never resent
automatically.
