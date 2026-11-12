"""Connection-scoped ACP dispatch to registered, server-owned sessions."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Generic, Literal

import acp
from acp import schema
from acp.interfaces import Client
from pydantic_ai.output import OutputDataT
from pydantic_ai.tools import AgentDepsT

from hrns.shared.acp_mcp import McpOverAcpCaller, bind_caller

from .._types import PrincipalT
from .content import PromptContentBlock
from .host import RegisteredAgent, Session
from .models import ModelChoice
from .runtime import ConfigOptions
from .session import McpServers
from .store import DeletableSessionStore, DocumentCheckpoint, ListableSessionStore, SessionAccess


class PydanticAIACPAgent(acp.Agent, Generic[PrincipalT, AgentDepsT, OutputDataT]):
    def __init__(
        self,
        registration: RegisteredAgent[PrincipalT, AgentDepsT, OutputDataT],
        *,
        principal: PrincipalT | None = None,
    ) -> None:
        self._principal = principal
        self._registration = registration
        self._session_store = registration.store
        self._model_resolver = registration.resolver
        self._conn: Client | None = None
        self._client_capabilities: schema.ClientCapabilities | None = None
        self._sessions: dict[str, Session[AgentDepsT, OutputDataT]] = {}
        self._name = registration.agent_id
        self._version = "0.1.0"
        self._mcp_capabilities = registration.mcp_capabilities
        self._prompt_capabilities = schema.PromptCapabilities()
        self._mcp_caller = McpOverAcpCaller()
        self._mcp_channel = None

    def on_connect(self, conn: Client) -> None:
        self._conn = conn
        self._mcp_channel = bind_caller(conn, self._mcp_caller)

    def detach(self) -> None:
        for session in self._sessions.values():
            if session._runtime._conn is self._conn:
                session._runtime.detach()
        self._sessions.clear()

    async def _load(self, session_id: str, operation: SessionAccess) -> DocumentCheckpoint:
        try:
            stored = await self._session_store.load(
                session_id, principal=self._principal, operation=operation
            )
        except PermissionError as exc:
            raise acp.RequestError(-32003, "Not authorized") from exc
        if stored is None:
            raise acp.RequestError.invalid_params({"session_id": session_id})
        return stored

    async def _session(
        self, session_id: str, operation: SessionAccess
    ) -> Session[AgentDepsT, OutputDataT]:
        await self._load(session_id, operation)
        session = self._sessions.get(session_id)
        if session is None or session._closed or session._runtime._conn is not self._conn:
            raise acp.RequestError.invalid_params(
                {"session_id": session_id, "reason": "session is not attached"}
            )
        return session

    async def _attach(
        self, session: Session[AgentDepsT, OutputDataT], mcp_servers: McpServers
    ) -> None:
        if self._conn is None:
            raise RuntimeError("ACP connection has not initialized")
        await session._runtime.attach(
            self._conn, self._client_capabilities, mcp_servers, self._mcp_caller, self._mcp_channel
        )
        self._sessions[session.session_id] = session

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: McpServers = None,
        **kwargs: object,
    ) -> schema.NewSessionResponse:
        try:
            session = await self._registration.new_session(cwd=cwd, principal=self._principal)
        except PermissionError as exc:
            raise acp.RequestError(-32003, "Not authorized") from exc
        runtime = session._runtime
        try:
            config_options = runtime._config_options(runtime.state)
            meta = await runtime._skills_meta() or {}
            await self._attach(session, mcp_servers)
            return schema.NewSessionResponse(
                session_id=session.session_id,
                config_options=config_options,
                field_meta={**meta, "hrns/session/state": session.status()},
            )
        except BaseException:
            await session.close()
            raise

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: McpServers = None,
        additional_directories: list[str] | None = None,
        **kwargs: object,
    ) -> schema.LoadSessionResponse:
        try:
            session = await self._registration.get_session(session_id, principal=self._principal)
        except KeyError as exc:
            raise acp.RequestError.invalid_params({"session_id": session_id}) from exc
        except PermissionError as exc:
            raise acp.RequestError(-32003, "Not authorized") from exc
        if str(Path(cwd).resolve()) != session.cwd:
            raise acp.RequestError.invalid_params({"reason": "session cwd does not match"})
        runtime = session._runtime
        config_options = runtime._config_options(runtime.state)
        meta = await runtime._skills_meta() or {}
        await self._attach(session, mcp_servers)
        return schema.LoadSessionResponse(
            config_options=config_options,
            field_meta={**meta, "hrns/session/state": session.status()},
        )

    async def prompt(
        self, session_id: str, prompt: list[PromptContentBlock], **kwargs: object
    ) -> schema.PromptResponse:
        session = await self._session(session_id, "session/prompt")
        if session.paused:
            raise acp.RequestError.invalid_params(
                {
                    "reason": "session is paused; resume it through Python or the Loro "
                    "resume route before prompting"
                }
            )
        handle = await session._accept(prompt, self._conn)
        result = await handle.result()
        return schema.PromptResponse(stop_reason=result.stop_reason, usage=result.usage)

    async def cancel(self, session_id: str, **kwargs: object) -> None:
        session = await self._session(session_id, "session/cancel")
        await session.cancel()

    async def close_session(self, session_id: str, **kwargs: object) -> schema.CloseSessionResponse:
        session = await self._session(session_id, "session/close")
        await session.close()
        self._sessions.pop(session_id, None)
        return schema.CloseSessionResponse()

    async def delete_session(
        self, session_id: str, **kwargs: object
    ) -> schema.DeleteSessionResponse:
        store = self._deletable_store
        if store is None or self._listable_store is None:
            raise acp.RequestError.method_not_found("session/delete")
        async with self._registration._lock:
            await self._load(session_id, "session/delete")
            session = self._registration._sessions.get(session_id)
            if session is not None:
                if session._runtime._conn is not None and session._runtime._conn is not self._conn:
                    raise acp.RequestError.invalid_params(
                        {"reason": "session has another ACP client"}
                    )
                await session._close()
                self._registration._sessions.pop(session_id, None)
                self._registration._views.pop(session_id, None)
                self._sessions.pop(session_id, None)
            try:
                await store.delete(session_id, principal=self._principal)
            except PermissionError as exc:
                raise acp.RequestError(-32003, "Not authorized") from exc
        return schema.DeleteSessionResponse()

    def _options_snapshot(self) -> dict[str, ConfigOptions | None]:
        return {
            sid: session._runtime._config_options(session._runtime.state)
            for sid, session in self._registration._sessions.items()
        }

    async def _push_option_changes(self, before: dict[str, ConfigOptions | None]) -> None:
        async with self._registration._lock:
            for sid, session in tuple(self._registration._sessions.items()):
                await session._runtime._push_option_changes(before.get(sid))

    @property
    def _listable_store(self) -> ListableSessionStore[PrincipalT] | None:
        store = self._session_store
        return store if isinstance(store, ListableSessionStore) else None

    @property
    def _deletable_store(self) -> DeletableSessionStore[PrincipalT] | None:
        store = self._session_store
        return store if isinstance(store, DeletableSessionStore) else None

    def _model_choices(self) -> Sequence[ModelChoice]:
        """The models advertised in the ``model`` config option, in priority order."""
        resolver = self._model_resolver
        return resolver.list_models() if resolver is not None else ()

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: schema.ClientCapabilities | None = None,
        client_info: schema.Implementation | None = None,
        **kwargs: object,
    ) -> schema.InitializeResponse:
        """Negotiate the protocol version and advertise the agent's capabilities."""
        self._client_capabilities = client_capabilities
        if 1 <= protocol_version <= acp.PROTOCOL_VERSION:
            negotiated_version = protocol_version
        else:
            negotiated_version = acp.PROTOCOL_VERSION
        session_capabilities = schema.SessionCapabilities(close=schema.SessionCloseCapabilities())
        if self._listable_store is not None:
            session_capabilities.list = schema.SessionListCapabilities()
            if self._deletable_store is not None:
                session_capabilities.delete = schema.SessionDeleteCapabilities()
        return schema.InitializeResponse(
            protocol_version=negotiated_version,
            agent_capabilities=schema.AgentCapabilities(
                load_session=self._session_store is not None,
                prompt_capabilities=self._prompt_capabilities,
                mcp_capabilities=self._mcp_capabilities,
                providers=(
                    schema.ProvidersCapabilities() if self._model_resolver is not None else None
                ),
                session_capabilities=session_capabilities,
            ),
            agent_info=schema.Implementation(name=self._name, version=self._version),
            field_meta={"hrns/session/state": True},
        )

    async def list_sessions(
        self,
        cwd: str | None = None,
        cursor: str | None = None,
        **kwargs: object,
    ) -> schema.ListSessionsResponse:
        """Return one page of stored sessions and the next page's cursor."""
        store = self._listable_store
        if store is None:
            raise acp.RequestError.method_not_found("session/list")
        try:
            summaries, next_cursor = await store.list_sessions(
                principal=self._principal, cwd=cwd, cursor=cursor
            )
        except ValueError as exc:
            raise acp.RequestError.invalid_params({"cursor": cursor, "reason": str(exc)}) from exc
        except PermissionError as exc:
            raise acp.RequestError(-32003, "Not authorized") from exc
        return schema.ListSessionsResponse(
            sessions=[
                schema.SessionInfo(
                    session_id=summary.session_id,
                    cwd=summary.cwd,
                    title=summary.title,
                    updated_at=(
                        summary.updated_at.isoformat() if summary.updated_at is not None else None
                    ),
                )
                for summary in summaries
            ],
            next_cursor=next_cursor,
        )

    async def set_session_mode(
        self, session_id: str, mode_id: str, **kwargs: object
    ) -> schema.SetSessionModeResponse | None:
        """Modes are not implemented by this adapter."""
        raise acp.RequestError.method_not_found("session/set_mode")

    async def set_config_option(
        self, config_id: str, session_id: str, value: str | bool, **kwargs: object
    ) -> schema.SetSessionConfigOptionResponse | None:
        session = await self._session(session_id, "session/set_config_option")
        return await session._runtime.set_config_option(
            config_id=config_id, session_id=session_id, value=value, **kwargs
        )

    async def fork_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: McpServers = None,
        **kwargs: object,
    ) -> schema.ForkSessionResponse:
        """Forking is not implemented by this adapter."""
        raise acp.RequestError.method_not_found("session/fork")

    async def resume_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: McpServers = None,
        **kwargs: object,
    ) -> schema.ResumeSessionResponse:
        """Resuming is not implemented by this adapter; use ``session/load``."""
        raise acp.RequestError.method_not_found("session/resume")

    async def authenticate(
        self, method_id: str, **kwargs: object
    ) -> schema.AuthenticateResponse | None:
        """No authentication is required, so this is a no-op."""
        return None

    async def logout(self, **kwargs: object) -> schema.LogoutResponse:
        """No authentication is configured, so there is nothing to log out of."""
        return schema.LogoutResponse()

    async def did_open(
        self,
        session_id: str,
        uri: str,
        language_id: str,
        version: int,
        text: str,
        **kwargs: object,
    ) -> None:
        session = await self._session(session_id, "document/didOpen")
        return await session._runtime.did_open(
            session_id=session_id,
            uri=uri,
            language_id=language_id,
            version=version,
            text=text,
            **kwargs,
        )

    async def did_change(
        self,
        session_id: str,
        uri: str,
        version: int,
        content_changes: list[schema.TextDocumentContentChangeEvent],
        **kwargs: object,
    ) -> None:
        session = await self._session(session_id, "document/didChange")
        return await session._runtime.did_change(
            session_id=session_id,
            uri=uri,
            version=version,
            content_changes=content_changes,
            **kwargs,
        )

    async def did_close(self, session_id: str, uri: str, **kwargs: object) -> None:
        session = await self._session(session_id, "document/didClose")
        return await session._runtime.did_close(session_id=session_id, uri=uri, **kwargs)

    async def did_save(self, session_id: str, uri: str, **kwargs: object) -> None:
        session = await self._session(session_id, "document/didSave")
        return await session._runtime.did_save(session_id=session_id, uri=uri, **kwargs)

    async def did_focus(
        self,
        session_id: str,
        uri: str,
        version: int,
        position: schema.Position,
        visible_range: schema.Range,
        **kwargs: object,
    ) -> None:
        session = await self._session(session_id, "document/didFocus")
        return await session._runtime.did_focus(
            session_id=session_id,
            uri=uri,
            version=version,
            position=position,
            visible_range=visible_range,
            **kwargs,
        )

    async def list_providers(self, **kwargs: object) -> schema.ListProvidersResponse:
        """Report the providers a client may route and their current non-secret routing."""
        resolver = self._model_resolver
        if resolver is None:
            raise acp.RequestError.method_not_found("providers/list")
        return schema.ListProvidersResponse(
            providers=[
                schema.ProviderInfo(
                    provider_id=provider.provider_id,
                    supported=list(provider.supported),
                    required=provider.required,
                    current=(
                        schema.ProviderCurrentConfig(
                            api_type=provider.current.api_type,
                            base_url=provider.current.base_url,
                        )
                        if provider.current is not None
                        else None
                    ),
                )
                for provider in resolver.list_providers()
            ]
        )

    async def set_provider(
        self,
        provider_id: str,
        api_type: Literal["anthropic", "openai", "azure", "vertex", "bedrock"] | str,
        base_url: str,
        headers: dict[str, str] | None = None,
        **kwargs: object,
    ) -> schema.SetProviderResponse:
        """Replace one provider's routing with the client's own endpoint."""
        resolver = self._model_resolver
        if resolver is None:
            raise acp.RequestError.method_not_found("providers/set")
        before = self._options_snapshot()
        try:
            resolver.set_provider(provider_id, api_type, base_url, headers or {})
        except ValueError as exc:
            raise acp.RequestError.invalid_params(
                {"provider_id": provider_id, "reason": str(exc)}
            ) from exc
        await self._push_option_changes(before)
        return schema.SetProviderResponse()

    async def disable_provider(
        self, provider_id: str, **kwargs: object
    ) -> schema.DisableProviderResponse:
        """Stop routing through one provider, unless the resolver marks it required."""
        resolver = self._model_resolver
        if resolver is None:
            raise acp.RequestError.method_not_found("providers/disable")
        before = self._options_snapshot()
        try:
            resolver.disable_provider(provider_id)
        except ValueError as exc:
            raise acp.RequestError.invalid_params(
                {"provider_id": provider_id, "reason": str(exc)}
            ) from exc
        await self._push_option_changes(before)
        return schema.DisableProviderResponse()

    async def mcp_message(
        self,
        connection_id: str,
        method: str,
        params: dict[str, object] | None = None,
        **kwargs: object,
    ) -> object:
        raise acp.RequestError.method_not_found("mcp/message")

    async def notify_mcp(
        self,
        connection_id: str,
        method: str,
        params: dict[str, object] | None = None,
        **kwargs: object,
    ) -> None:
        return None

    async def start_nes(
        self,
        workspace_uri: str | None = None,
        workspace_folders: list[schema.WorkspaceFolder] | None = None,
        repository: schema.NesRepository | None = None,
        **kwargs: object,
    ) -> schema.StartNesResponse:
        """Unsupported: this adapter has no next-edit-suggestion service."""
        raise acp.RequestError.method_not_found("nes/start")

    async def suggest_nes(
        self,
        session_id: str,
        uri: str,
        version: int,
        position: schema.Position,
        trigger_kind: Literal["automatic", "diagnostic", "manual"],
        selection: schema.Range | None = None,
        context: schema.NesSuggestContext | None = None,
        **kwargs: object,
    ) -> schema.SuggestNesResponse:
        """Unsupported: this adapter has no next-edit-suggestion service."""
        raise acp.RequestError.method_not_found("nes/suggest")

    async def close_nes(self, session_id: str, **kwargs: object) -> schema.CloseNesResponse:
        """Unsupported: this adapter has no next-edit-suggestion service."""
        raise acp.RequestError.method_not_found("nes/close")

    async def accept_nes(self, session_id: str, id: str, **kwargs: object) -> None:  # noqa: A002
        """Unsupported: NES acceptance notifications are ignored."""
        return None

    async def reject_nes(
        self,
        session_id: str,
        id: str,  # noqa: A002
        reason: Literal["rejected", "ignored", "replaced", "cancelled"] | None = None,
        **kwargs: object,
    ) -> None:
        """Unsupported: NES rejection notifications are ignored."""
        return None

    async def ext_method(self, method: str, params: dict[str, object]) -> dict[str, object]:
        """This adapter exposes no extension methods."""
        raise acp.RequestError.method_not_found(method)

    async def ext_notification(self, method: str, params: dict[str, object]) -> None:
        """This adapter exposes no extension notifications."""
        return None
