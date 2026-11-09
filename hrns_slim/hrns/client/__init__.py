"""The standalone hrns client: ACP terminal and headless runner.

This package is independent of ``hrns.server``. It speaks standard ACP v1 over
stdio or a WebSocket. The UI is Textual; the protocol layer is ``acp``.

Imports are intentionally not re-exported here: the base install must be able to
import ``hrns.client.launch`` and ``hrns.client.runtime`` without the client extra.
"""
