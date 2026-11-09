"""hrns: one Pydantic AI agent, exposed over ACP.

The distribution exposes three subpackages:

- ``hrns.client``: the Textual terminal, standard ACP client, and built-in agent launcher.
- ``hrns.server``: the agent composition and the ACP/FastAPI adapters.
- ``hrns.shared``: configuration, credentials, MCP-over-ACP, errors, and URL helpers.

``hrns.cli`` is the unified command line. Importing ``hrns`` does not import any
subpackage.
"""
