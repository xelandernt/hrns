# Workspaces

A workspace is a native Pydantic AI workspace backend bound through a definition resource.
In this revision Harness supports a **local path** workspace: the ACP session's canonical
`cwd` is stored in the resource named by the definition's `workspace` requirement, and
`resolve_workspace_value` maps it to `pydantic_ai.workspaces.LocalWorkspaceBackend`.

```python
definition = AgentDefinition(
    agent_id="developer",
    name="Developer",
    resources=[ResourceRequirement(name="workspace", kind="workspace")],
    ...
)
```

At session creation:

1. the host canonicalizes the client's `cwd` and validates it against allowed roots;
2. the cwd becomes the `workspace` resource unless the deployment bound a richer value;
3. `compose_definition` resolves the resource and passes the backend through
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
bound upstream backend (for example an SSH or cloud workspace supplied by an installed
plugin); a requested unavailable backend fails rather than falling back to local execution.

## Workspace-dependent capabilities

Capabilities that need a workspace derive their working directory from the bound resource.
They do not inspect the client machine. A definition that enables such a binding must declare
and receive the workspace resource, or session composition fails before any turn starts.
