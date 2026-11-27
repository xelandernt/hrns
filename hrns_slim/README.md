# hrns-slim

<p align="center">
  <img src="https://raw.githubusercontent.com/xelandernt/hrns/main/docs/assets/hrns.svg" alt="hrns logo" width="128" height="128">
</p>

Minimal installation of [hrns](https://github.com/xelandernt/hrns).

```sh
pip install 'hrns-slim[client]>0.0.28'  # terminal client
pip install 'hrns-slim[headless]>0.0.28'  # scripted prompts with hrns run
pip install 'hrns-slim[server]'  # agent and HTTP server
```

Install the full package with `pip install 'hrns>0.0.28'`. Add the `ag-ui` extra for
observation support. Python imports and the command are named `hrns` in both packages.

Without extras, `hrns-slim` provides the shared configuration, profile, and MCP
configuration commands.
