# hrns-slim

<p align="center">
  <img src="https://raw.githubusercontent.com/xelandernt/hrns/main/docs/assets/hrns.svg" alt="hrns logo" width="128" height="128">
</p>

Minimal installation of [hrns](https://github.com/xelandernt/hrns).

```sh
pip install 'hrns-slim[client]'  # terminal client
pip install 'hrns-slim[server]'  # agent and HTTP server
```

Install `hrns` for both client and server by default. The `server` extra includes
the Python Loro SDK for session documents. Add the `ag-ui` extra for observation
support. Python imports and the command are named `hrns` in both packages.

Without extras, `hrns-slim` provides the shared configuration, profile, and MCP
configuration commands.
