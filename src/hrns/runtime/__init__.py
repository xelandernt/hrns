"""hrns runtime: native agent composition, plugin registry, and ACP persistence.

Public integration surface::

    from hrns.runtime import AgentDefinition, PluginRegistry, ResourceSet
    from hrns.runtime.composition import compose_definition

ACP hosting lives in :mod:`hrns.runtime.host` and is not imported by this package, so
Python callers do not pull in the ACP adapter.
"""

from __future__ import annotations

from hrns.protocol import (
    AddChange,
    BindingChange,
    BindingDescriptor,
    BindingState,
    CapabilityPolicy,
    ConfigurationSnapshot,
    ConfigurationUpdate,
    ConfigureChange,
    EnableChange,
    HrnsError,
    PluginInfo,
)

from . import _warnings  # noqa: F401  (the pinned ACP adapter is experimental)
from ._version import __version__
from .acp_store import ScopedSessionStore, SessionRecord, SessionStore
from .composition import Composition, EffortState, compose_definition
from .definitions import (
    AGENT_FACTORY_PARAM,
    AgentDefinition,
    AgentFactory,
    AgentFactoryContext,
    AgentSpec,
    BoundDefinition,
    CapabilityFactory,
    DefinitionBinding,
    DefinitionManifest,
    DefinitionManifestBinding,
    DefinitionManifestResource,
    definition_to_manifest,
    factory_managed_param,
    load_agent_spec,
    manifest_from_file,
    parse_manifest_data,
    parse_manifest_text,
)
from .deps import HrnsDeps
from .extensions import Command, CommandBinding, CommandContext, TextArguments
from .policy import PolicyCatalog, changes_payload_hash, order_enabled_offerings
from .registry import (
    ENTRY_POINT_GROUP,
    BindingContext,
    CompatibilityConstraints,
    EditableField,
    NonEditableField,
    PluginDescriptor,
    PluginRegistry,
)
from .resources import (
    ANSWERS,
    MODEL,
    PLAN,
    SLOTS,
    WORKSPACE,
    ModelInput,
    ResolvedWorkspace,
    ResourceBindings,
    ResourceKind,
    ResourceRequirement,
    ResourceSet,
    ResourceSlot,
    WorkspaceInput,
)
from .store import SQLiteSessionStore, default_store_path

__all__ = [
    # protocol models used in signatures
    "AddChange",
    "BindingChange",
    "BindingDescriptor",
    "BindingState",
    "CapabilityPolicy",
    "ConfigurationSnapshot",
    "ConfigurationUpdate",
    "ConfigureChange",
    "EnableChange",
    "HrnsError",
    "PluginInfo",
    # command authoring
    "Command",
    "CommandBinding",
    "CommandContext",
    "TextArguments",
    # definitions
    "AGENT_FACTORY_PARAM",
    "AgentDefinition",
    "BoundDefinition",
    "AgentFactory",
    "AgentFactoryContext",
    "AgentSpec",
    "CapabilityFactory",
    "DefinitionBinding",
    "DefinitionManifest",
    "DefinitionManifestBinding",
    "DefinitionManifestResource",
    "definition_to_manifest",
    "factory_managed_param",
    "load_agent_spec",
    "manifest_from_file",
    "parse_manifest_data",
    "parse_manifest_text",
    # plugins
    "BindingContext",
    "CompatibilityConstraints",
    "ENTRY_POINT_GROUP",
    "EditableField",
    "NonEditableField",
    "PluginDescriptor",
    "PluginRegistry",
    # policy
    "PolicyCatalog",
    "changes_payload_hash",
    "order_enabled_offerings",
    # resources
    "ANSWERS",
    "MODEL",
    "PLAN",
    "SLOTS",
    "WORKSPACE",
    "ModelInput",
    "ResolvedWorkspace",
    "ResourceBindings",
    "ResourceKind",
    "ResourceRequirement",
    "ResourceSet",
    "ResourceSlot",
    "WorkspaceInput",
    # composition
    "Composition",
    "EffortState",
    "HrnsDeps",
    "compose_definition",
    # persistence
    "SQLiteSessionStore",
    "ScopedSessionStore",
    "SessionRecord",
    "SessionStore",
    "default_store_path",
    "__version__",
]
