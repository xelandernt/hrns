# Workspaces

A workspace is a native Pydantic AI workspace backend bound through a definition resource.
In this revision Harness supports a **local path** workspace: the ACP session's canonical
`cwd` is stored in the resource named by the definition's `workspace` requirement, and
`resolve_workspace_value` maps it to `pydantic_ai.workspaces.LocalWorkspaceBackend`.

```python
from harness.runtime import WORKSPACE, ResourceRequirement

definition = AgentDefinition(
    agent_id="developer",
    name="Developer",
    resources=[ResourceRequirement.of(WORKSPACE)],
    ...
)
```

`ResourceRequirement` is a declaration, never a value: it serializes into a manifest and is
evaluated against whatever deployment opens the session. Capabilities read the value through
the typed slot — `resources.get(WORKSPACE)` — so a misspelled resource cannot silently become
`None`, and a value of the wrong type is rejected when the deployment binds it.

At session creation:

1. the host canonicalizes the client's `cwd` and validates it against allowed roots;
2. the cwd fills the `workspace` slot only when the deployment bound no value of its own;
3. `compose_definition` resolves the slot and passes the backend through
   `AcpSessionConfig.workspace` to every run;
4. the recorded cwd is persisted for reload and list.

`session/load` uses the recorded cwd and ignores the client's requested value. A recorded
path outside the current allowed roots fails closed.

## Lifecycle

`LocalWorkspaceBackend` owns no external lease. Session close cancels active work through the
upstream adapter; Harness does not tear down the local filesystem. Sandbox and remote
workspace backends can be supplied as deployment resources, but their lifecycle remains the
deployment's responsibility. Upstream `AcpSessionConfig` does not close workspace backends.

The previous `WorkspaceManager`/lease/job subsystem was removed. There are no background-job
records, named persistent leases, or workspace state tables in the ACP store.

## Isolation boundary

A local workspace is **not a sandbox**. Function tools such as filesystem and shell execute in
the host process under its user's permissions. Isolation requires an explicitly installed and
bound upstream backend; a requested unavailable backend fails rather than falling back to
local execution.

Pydantic AI Harness ships the remote and sandboxed implementations, so a deployment binds one
instead of writing one: `SSHWorkspaceBackend` (or the `SSHWorkspace` capability) for another
machine, and `DockerSandbox`, `E2BSandbox`, `ModalSandbox`, `DaytonaSandbox`,
`SpritesSandbox`, `BubblewrapSandbox`, `boxlite_sandbox` and `smol_sandbox` for isolated
environments. Each is a `WorkspaceBackend`, so binding it to the `workspace` slot is the whole
integration:

```python
from pydantic_ai_harness.ssh_workspace import SSHWorkspaceBackend
from harness.runtime import MODEL, WORKSPACE

bound = definition.bind(
    {MODEL: model, WORKSPACE: SSHWorkspaceBackend("dev@build-01", working_dir="/srv/app")}
)
agents.add_agent(
    bound,
    allowed_cwds=["/srv/app"],
    default_cwd="/srv/app",
)
```

A workspace-dependent capability's `needs` also becomes a definition requirement, so a
definition with the coder binding cannot be bound without a workspace. `bind` validates the
backend against the slot and `agents.add_agent` refuses an unbound definition.

## Workspace-dependent capabilities

Capabilities that need a workspace derive their working directory from the bound resource.
They do not inspect the client machine. A definition that enables such a binding must declare
and receive the workspace resource, or session composition fails before any turn starts.
