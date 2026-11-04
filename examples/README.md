# Examples

- `fastapi_app.py` — one Pydantic AI agent mounted over ACP WebSocket.

```sh
HRNS_MODEL=openai:gpt-6.1-sol uvicorn examples.fastapi_app:app --reload
```

- ACP: `ws://127.0.0.1:8000/acp`
