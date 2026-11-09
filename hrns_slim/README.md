# hrns-slim

Minimal installation of [hrns](https://github.com/xelandernt/hrns).

```sh
pip install 'hrns-slim[client]'  # terminal client
pip install 'hrns-slim[server]'  # agent and HTTP server
```

Install `hrns` for both client and server by default. Add the `ag-ui` extra for
observation support. Python imports and the command are named `hrns` in both packages.

Without extras, `hrns-slim` provides the shared configuration, profile, and MCP
configuration commands.
