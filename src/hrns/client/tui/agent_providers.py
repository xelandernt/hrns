"""Change the connected agent's LLM provider routing through ACP's ``providers/*``.

These entries live in the agent process, not in ``hrns.local.toml``: the agent keeps them
in memory, applies them to models it resolves afterwards, and never returns the headers a
client sent. The form is therefore the only place a header value is visible once it is
submitted, and the table can only show the routing the agent is willing to report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Literal, Mapping, Sequence

from acp.schema import ProviderInfo
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import DataTable, Static

from .dialogs import ConfirmDialog, FormDialog, _collect_fields, _DialogBase
from .state import value_provider

__all__ = [
    "AgentProviderFormDialog",
    "AgentProviderRequest",
    "AgentProvidersDialog",
    "models_by_provider",
]


def models_by_provider(
    config_options: Sequence[Mapping[str, Any]],
) -> dict[str, list[str]]:
    """Group the session's model ids by the provider each one is tagged with.

    ACP's ``providers/*`` payload describes endpoints, so the link to models is carried
    as ``_meta.providerId`` on the model config option's values. Agents that do not tag
    them simply contribute nothing, and the table shows no models for any provider.
    """
    grouped: dict[str, list[str]] = {}
    option = next(
        (
            item
            for item in config_options
            if item.get("id") == "model" or item.get("category") == "model"
        ),
        None,
    )
    if option is None:
        return grouped
    for value in _select_values(option.get("options")):
        provider_id = value_provider(dict(value))
        if not provider_id:
            continue
        name = str(value.get("name") or value.get("value") or "")
        grouped.setdefault(provider_id, []).append(name)
    return grouped


def _select_values(options: object) -> Iterator[Mapping[str, Any]]:
    """Flat select values, or a group's values, whichever the agent sent."""
    if not isinstance(options, Sequence):
        return
    for item in options:
        if not isinstance(item, Mapping):
            continue
        grouped = item.get("options")
        if isinstance(grouped, Sequence):
            yield from _select_values(grouped)
        else:
            yield item


@dataclass(frozen=True, slots=True)
class AgentProviderRequest:
    """What the provider table asked the agent to do."""

    kind: Literal["set", "disable"]
    provider_id: str
    api_type: str = ""
    base_url: str = ""
    headers: dict[str, str] = field(default_factory=dict)


def _field(
    name: str,
    title: str,
    *,
    description: str = "",
    kind: str = "text",
    default: Any = "",
    choices: list[tuple[str, str]] | None = None,
    required: bool = False,
) -> dict[str, Any]:
    return {
        "name": name,
        "title": title,
        "description": description,
        "kind": kind,
        "choices": choices or [],
        "required": required,
        "default": default,
    }


def parse_headers(text: str) -> dict[str, str]:
    """Read ``Name: value`` pairs into a header map, rejecting malformed ones.

    The form row is a single line, so pairs are separated by ``;``; a newline works too.
    """
    headers: dict[str, str] = {}
    for number, part in enumerate(text.replace("\n", ";").split(";"), start=1):
        entry = part.strip()
        if not entry:
            continue
        name, separator, value = entry.partition(":")
        name = name.strip()
        if not separator or not name:
            raise ValueError(f"Header {number} must read 'Name: value'.")
        headers[name] = value.strip()
    return headers


class AgentProviderFormDialog(FormDialog):
    """Set one provider's API type, base URL and headers in the agent process."""

    def __init__(self, provider: ProviderInfo) -> None:
        self.provider_id = provider.provider_id
        supported = list(provider.supported) or ["openai"]
        current = provider.current
        super().__init__(
            f"Route {provider.provider_id}",
            "Applies to models this agent resolves after the change",
            [
                _field(
                    "api_type",
                    "API type",
                    kind="select",
                    description="The protocol this provider speaks; the agent rejects others.",
                    choices=[(api_type, api_type) for api_type in supported],
                    default=current.api_type if current is not None else supported[0],
                    required=True,
                ),
                _field(
                    "base_url",
                    "Base URL",
                    description="Where this provider's requests go, for example a gateway.",
                    default=current.base_url if current is not None else "",
                    required=True,
                ),
                _field(
                    "headers",
                    "Headers",
                    description=(
                        "Optional 'Name: value' pairs separated by ';'. They replace the "
                        "headers the agent holds, and the agent never reports them back."
                    ),
                ),
            ],
            accept_label="Route",
            explanation=(
                "The agent applies this to sessions it creates or reloads afterwards. A "
                "session that is already running keeps the endpoint it started with."
            ),
        )

    def action_accept_form(self) -> None:
        values, error = _collect_fields(self.fields, self._rows_by_name)
        if error or values is None:
            self.show_error(error or "The form has no values.")
            return
        base_url = str(values.get("base_url") or "").strip()
        if not base_url.lower().startswith(("http://", "https://")):
            self.show_error("The base URL must start with http:// or https://.")
            return
        try:
            headers = parse_headers(str(values.get("headers") or ""))
        except ValueError as exc:
            self.show_error(str(exc))
            return
        self.dismiss(
            AgentProviderRequest(
                kind="set",
                provider_id=self.provider_id,
                api_type=str(values.get("api_type") or ""),
                base_url=base_url,
                headers=headers,
            )
        )


class AgentProvidersDialog(_DialogBase):
    """The agent's configurable providers, and the routing each one reports."""

    BINDINGS = [
        Binding("escape", "cancel", "Close", show=False),
        Binding("s", "set", "Route", show=False),
        Binding("enter", "set", "Route", show=False, priority=True),
        Binding("d", "disable", "Disable", show=False),
    ]

    def __init__(
        self,
        providers: list[ProviderInfo],
        *,
        models: Mapping[str, Sequence[str]] | None = None,
        subtitle: str = "",
    ) -> None:
        super().__init__(
            "Agent providers",
            subtitle or "Configured in the agent process for this connection only",
        )
        self.providers = providers
        self.models = dict(models or {})
        self._ids: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog dialog-tall"):
            yield from self.dialog_header()
            yield Static(
                "The agent holds these in memory: they are not saved to a config file and "
                "disappear when it stops. Header values are never reported back. Models are "
                "the ones the agent tags with this provider id.",
                markup=False,
                classes="dialog-help",
            )
            yield self._error
            yield DataTable(id="agent-provider-table", cursor_type="row", zebra_stripes=True)
            yield Static(
                "↑/↓ move · s route · d disable · Esc close",
                markup=False,
                classes="dialog-hint",
            )

    def on_mount(self) -> None:
        table = self.query_one("#agent-provider-table", DataTable)
        table.add_columns("Provider", "API types", "Required", "Routing", "Models")
        self._fill()
        table.focus()

    def _fill(self) -> None:
        table = self.query_one("#agent-provider-table", DataTable)
        table.clear()
        self._ids = []
        for provider in self.providers:
            current = provider.current
            routing = (
                f"{current.api_type} · {current.base_url or 'provider default'}"
                if current is not None
                else "disabled"
            )
            table.add_row(
                provider.provider_id,
                ", ".join(provider.supported) or "—",
                "yes" if provider.required else "",
                routing,
                ", ".join(self.models.get(provider.provider_id, ())) or "—",
                key=provider.provider_id,
            )
            self._ids.append(provider.provider_id)
        if not self._ids:
            table.add_row("no configurable providers", "", "", "", "", key="__empty__")

    def _focused_provider(self) -> ProviderInfo | None:
        if not self._ids:
            return None
        table = self.query_one("#agent-provider-table", DataTable)
        index = table.cursor_row
        return self.providers[index] if 0 <= index < len(self.providers) else None

    def action_set(self) -> None:
        provider = self._focused_provider()
        if provider is None:
            self.show_error("This agent reports no configurable providers.")
            return
        self.app.push_screen(AgentProviderFormDialog(provider), self._routed)

    def _routed(self, request: AgentProviderRequest | None) -> None:
        if request is not None:
            self.dismiss(request)

    def action_disable(self) -> None:
        provider = self._focused_provider()
        if provider is None:
            self.show_error("This agent reports no configurable providers.")
            return
        if provider.required:
            self.show_error(
                f"{provider.provider_id} is required, so this agent will not disable it."
            )
            return
        self.app.push_screen(
            ConfirmDialog(
                "Disable provider",
                f"Stop routing {provider.provider_id} through this agent until it is "
                "configured again? Models that use it will fail.",
                confirm_label="Disable",
            ),
            lambda confirmed: self._disabled(provider, bool(confirmed)),
        )

    def _disabled(self, provider: ProviderInfo, confirmed: bool) -> None:
        if confirmed:
            self.dismiss(AgentProviderRequest(kind="disable", provider_id=provider.provider_id))
