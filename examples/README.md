# Examples

`fastapi_app.py` registers a factory with `HrnsServer` and mounts authenticated ACP
and read-only AG-UI routes for the same live sessions.

```sh
uv sync --extra client --extra server --extra ag-ui
DEEPSEEK_API_KEY=... uvicorn examples.fastapi_app:app --reload
hrns connect --server ws://127.0.0.1:8000/acp/coder --token example-token --client-io all
```

ACP is at `/acp/coder`; observation is at `/ag-ui/coder/sessions/{session_id}` and
`/ag-ui/coder/sessions/{session_id}/events`. Both require `Authorization: Bearer example-token`.

The agent installs the ACP client filesystem and terminal toolsets through
`attachment_config`, so `--client-io all` gives it `read_file`, `write_file`, and
`run_command` on the machine running `hrns`. With `--client-io read` it gets
`read_file`; `write_file` falls back to the server's filesystem instead of the
client's. Without that hook, `client_io` would advertise capabilities the agent
never uses.
