# Harness

Define an agent once. Run it in Python, open it in a terminal, or host it remotely.

Harness combines native Pydantic AI capabilities with policy-controlled sessions,
an independent ACP terminal, and authenticated remote hosting.

## Get started

- [Configure a local agent](LOCAL.md) and connect it to your model.
- [Use the terminal or Python client](CLIENT_API.md).
- [Define agents and plugins](RUNTIME_API.md).
- [Host agents in FastAPI](SERVER.md) or [use the ACP host](ACP.md).

## Understand the system

- [Architecture](ARCHITECTURE.md) and [shared contracts](CONTRACTS.md)
- [Extension protocol](PROTOCOL.md) and [extension authoring](EXTENSIONS.md)
- [Security model](SECURITY.md) and [verification boundaries](VERIFICATION.md)

The **API reference** section is generated from the Python source and docstrings.
For AI tools, the build also provides an [llms.txt index](llms.txt) and
[combined documentation](llms-full.txt).
