"""Harness runtime: typed definitions, native capability composition, and durable sessions.

Public integration surface (see ``docs/RUNTIME_API.md``)::

    from harness.runtime import SessionManager, PluginRegistry, AgentDefinition, ResourceSet

The ACP host, catalog, and starters are optional modules layered on this core
(``harness.runtime.acp``, ``.host``, ``.catalog``, ``.starters``); importing this package
never imports them, so Python callers do not pull in ACP or catalog dependencies.
"""

from __future__ import annotations

from harness.protocol import (
    AddChange,
    BindingChange,
    BindingDescriptor,
    BindingState,
    CapabilityPolicy,
    ConfigurationSnapshot,
    ConfigurationUpdate,
    ConfigureChange,
    EnableChange,
    HarnessError,
    PluginInfo,
    RuntimeEvent,
)

from ._version import __version__
from .definitions import (
    AGENT_FACTORY_PARAM,
    AgentDefinition,
    AgentFactory,
    AgentFactoryContext,
    AgentSpec,
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
from .jobs import OwnedJob, SessionJobManager
from .policy import PolicyCatalog, changes_payload_hash, order_enabled_offerings
from .registry import (
    ENTRY_POINT_GROUP,
    BindingContext,
    CompatibilityConstraints,
    PluginDescriptor,
    PluginRegistry,
)
from .resources import ResourceKind, ResourceRequirement, ResourceSet
from .session import (
    HarnessRuntime,
    Session,
    SessionManager,
    SessionOpener,
    SessionSummary,
    WorkspaceBinding,
)
from .store import (
    SessionRecord,
    SessionStore,
    StoredRevision,
    StoredTurn,
    TransactionClaim,
    default_store_path,
)

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
    "HarnessError",
    "PluginInfo",
    "RuntimeEvent",
    # session-owned work
    "OwnedJob",
    "SessionJobManager",
    # definitions
    "AGENT_FACTORY_PARAM",
    "AgentDefinition",
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
    "PluginDescriptor",
    "PluginRegistry",
    # policy
    "PolicyCatalog",
    "changes_payload_hash",
    "order_enabled_offerings",
    # resources
    "ResourceKind",
    "ResourceRequirement",
    "ResourceSet",
    # sessions
    "HarnessRuntime",
    "Session",
    "SessionManager",
    "SessionOpener",
    "SessionSummary",
    "WorkspaceBinding",
    # persistence
    "SessionRecord",
    "SessionStore",
    "StoredRevision",
    "StoredTurn",
    "TransactionClaim",
    "default_store_path",
    "__version__",
]
