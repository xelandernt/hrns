"""hrns: programmable Pydantic AI agents with an ACP terminal and hosting.

The distribution exposes independent subpackages:

- ``hrns.protocol``: versioned contracts and transport framing.
- ``hrns.config``: shared local settings.
- ``hrns.runtime``: native agent definitions, sessions, and ACP hosting.
- ``hrns.client``: Textual terminal, headless runner, and stdio bridge.
- ``hrns.server``: authenticated deployment catalog and worker hosting.

Importing ``hrns`` does not import any subpackage, so the client and server
keep their independent dependency boundaries.
"""
