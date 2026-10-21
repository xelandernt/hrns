"""Version-one, non-executable metadata shared by hosts and clients."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, model_validator
from pydantic.alias_generators import to_camel


class WireModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    def to_wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


BindingId = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9_.:-]+$")]


class EnableChange(WireModel):
    type: Literal["enable"] = "enable"
    binding_id: BindingId
    enabled: bool


class ConfigureChange(WireModel):
    type: Literal["configure"] = "configure"
    binding_id: BindingId
    fields: dict[str, JsonValue]


class AddChange(WireModel):
    type: Literal["add"] = "add"
    binding_id: BindingId
    plugin_id: BindingId
    config: dict[str, JsonValue] = Field(default_factory=dict)
    enabled: bool = True


BindingChange = Annotated[EnableChange | ConfigureChange | AddChange, Field(discriminator="type")]
changes_adapter = TypeAdapter(list[BindingChange])


class BindingState(WireModel):
    enabled: bool
    config: dict[str, JsonValue] = Field(default_factory=dict)


class ConfigurationSnapshot(WireModel):
    schema_version: Literal[1] = 1
    session_id: str
    selected_revision: int = Field(default=0, ge=0)
    active_revision: int | None = Field(default=None, ge=0)
    policy_revision: int = Field(default=1, ge=1)
    application_status: Literal["applied", "pending", "failed"] = "pending"
    bindings: dict[str, BindingState] = Field(default_factory=dict)


class ConfigurationUpdate(WireModel):
    schema_version: Literal[1] = 1
    session_id: str
    request_id: str = Field(min_length=1, max_length=200)
    expected_revision: int = Field(ge=0)
    changes: list[BindingChange] = Field(min_length=1, max_length=100)


class CapabilityPolicy(WireModel):
    available: bool = True
    enabled_by_default: bool = True
    client_can_toggle: bool = False
    editable_fields: set[str] = Field(default_factory=set)
    change_mode: Literal["next_turn", "new_session"] = "new_session"


class BindingDescriptor(WireModel):
    schema_version: Literal[1] = 1
    binding_id: BindingId
    plugin_id: str | None = None
    name: str = ""
    description: str = ""
    version: str = ""
    config_schema: dict[str, JsonValue] = Field(default_factory=dict)
    config: dict[str, JsonValue] = Field(default_factory=dict)
    defaults: dict[str, JsonValue] = Field(default_factory=dict)
    enabled: bool = False
    active_enabled: bool | None = None
    policy: CapabilityPolicy = Field(default_factory=CapabilityPolicy)
    change_mode: Literal["next_turn", "new_session"] = "new_session"
    state_mode: Literal["run", "session", "external"] = "run"
    requires: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    availability_reason: str | None = None
    presentation: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def effective_lifecycle(self) -> "BindingDescriptor":
        # policy is authoritative; the top-level field is a convenience mirror
        # for generic descriptor renderers, never an independent permission.
        self.change_mode = self.policy.change_mode
        return self


class PluginInfo(WireModel):
    schema_version: Literal[1] = 1
    plugin_id: str
    version: str
    description: str = ""
    config_schema: dict[str, JsonValue] = Field(default_factory=dict)
    change_mode: Literal["next_turn", "new_session"] = "new_session"
    state_mode: Literal["run", "session", "external"] = "run"
    runtime_compatible: bool = True
    managed: bool = False
    presentation: str = "generic"
    requirements: list[str] = Field(default_factory=list)
    constraint: str | None = None


class ListRequest(WireModel):
    schema_version: Literal[1] = 1
    session_id: str | None = None
    cursor: str | None = None
    limit: int = Field(default=50, ge=1, le=100)


class BindingListResponse(WireModel):
    schema_version: Literal[1] = 1
    items: list[BindingDescriptor] = Field(default_factory=list)
    next_cursor: str | None = None
    selected_revision: int = Field(default=0, ge=0)
    active_revision: int | None = Field(default=None, ge=0)
    policy_revision: int = Field(default=1, ge=1)


class PluginListResponse(WireModel):
    schema_version: Literal[1] = 1
    items: list[PluginInfo] = Field(default_factory=list)
    next_cursor: str | None = None


class PresentationPayload(WireModel):
    renderer_id: str = Field(min_length=1, max_length=200)
    schema_version: int = Field(default=1, ge=1)
    data: dict[str, JsonValue]
    fallback: str


class ExtensionAdvertisement(WireModel):
    version: Literal[1] = 1
    methods: list[str]
    local_binding_creation: bool = False


class RuntimeEvent(WireModel):
    type: str
    data: dict[str, Any] = Field(default_factory=dict)
