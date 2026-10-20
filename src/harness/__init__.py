"""Harness: programmable Pydantic AI agents with an ACP terminal and hosting.

The distribution exposes independent subpackages:

- ``harness.protocol``: versioned contracts and transport framing.
- ``harness.config``: shared local settings.
- ``harness.runtime``: native agent definitions, sessions, and ACP hosting.
- ``harness.client``: Textual terminal, headless runner, and stdio bridge.
- ``harness.server``: authenticated deployment catalog and worker hosting.

Importing ``harness`` does not import any subpackage, so the client and server
keep their independent dependency boundaries.
"""
