# Capabilities

`build_agent` composes an agent from [Pydantic AI capabilities](https://ai.pydantic.dev/capabilities/).
The capabilities below provide coding tools, task tracking, skills, user questions, and
approval handling.

| Capability | Import | Purpose |
| --- | --- | --- |
| [Coder](#coder) | `hrns.server.capabilities.Coder` | File tools, shell commands, delegation, and context management |
| [ApplyChanges](#apply_changes) | `hrns.server.capabilities.changes.ApplyChanges` | Batched file writes and edits; included in `Coder` |
| [Todo](#todo) | `hrns.server.capabilities.Todo` | Task lists and `TodoUpdated` events |
| [Skills](#skills) | `hrns.server.capabilities.Skills` | Skill discovery, a host catalog, and `$name` invocation |
| [AskUser](#ask-user) | `hrns.server.capabilities.AskUser` | Questions delivered through ACP form elicitation |
| [Approval](#approval) | `hrns.server.approvals.require_approval` | Tool approval policy |

## Coder

`Coder` provides tools for reading, editing, and searching a project, running commands,
and delegating tasks. Bind a workspace alongside it to set the project directory:

```python
from pydantic_ai import Agent
from pydantic_ai.capabilities import LocalWorkspace

from hrns.server.capabilities import Coder

agent = Agent(
    "anthropic:claude-sonnet-5-5",
    capabilities=[
        LocalWorkspace("."),
        Coder(instructions="Run the project's lint command after editing code."),
    ],
)
```

### Arguments

| Argument | Type | Default | Purpose |
| --- | --- | --- | --- |
| `instructions` | `str \| None` | `None` | Additional coding instructions, appended to the built-in guidance |
| `unrestricted_filesystem` | `bool` | `False` | Allow the file tools to access the workspace filesystem from `/`; the default confines them to the working directory and applies protected-path rules |
| `repo_context` | `bool` | `True` | Load project instructions and repository context at the start of a run |
| `sub_agents` | `bool` | `True` | Add `delegate_task` for fresh runs of the same agent; bind `Coder` on `Agent(capabilities=...)` to use delegation |
| `agent_folders` | `str \| Sequence[str \| Path] \| None` | `None` | Discover disk-defined delegates from folder names or explicit workspace paths when `sub_agents=True` |
| `caching` | `bool` | `True` | Enable prompt caching as a default; explicit model, agent, capability, and run settings take precedence |

The workspace supplies file access and command execution. Use `LocalWorkspace(path)` for
local work or a sandbox workspace capability for isolated execution. Shell commands run
with the workspace's permissions and can outlive an agent run. Approval is configured
separately through [`require_approval`](#approval), which `build_agent` includes by default.
The protected-path checks for [`apply_changes`](#apply_changes) apply independently of
`unrestricted_filesystem`.

### Tools and context

| Tool | Purpose |
| --- | --- |
| `read_file` | Read file contents |
| `write_file` | Create a file or replace its contents |
| `edit_file` | Apply exact text replacements to a file |
| `list_files` | List workspace files |
| `grep` | Search file contents |
| `shell` | Run commands in the workspace |
| `delegate_task` | Delegate a task when `sub_agents=True` |
| `apply_changes` | Apply a batch of file writes and edits under one approval |

`Coder` also loads repository context, clears older tool results as context fills,
bounds tool output, and repairs malformed tool arguments. Its coding configuration is
described in the [Coder documentation](https://pydantic.dev/docs/ai/harness/coder/).

### `apply_changes`

`apply_changes` accepts up to 64 file writes and edits in one call:

```python
apply_changes(
    changes=[
        {"kind": "write", "path": "src/app/main.py", "content": "..."},
        {
            "kind": "edit",
            "path": "src/app/settings.py",
            "expected_hash": "4205c4809ab1",
            "replacements": [{"old_text": "TIMEOUT = 30", "new_text": "TIMEOUT = 60"}],
        },
    ]
)
```

Each entry is one of two shapes:

| `kind` | Fields | Meaning |
| --- | --- | --- |
| `write` | `path`, `content` | Create the file or replace its complete contents |
| `edit` | `path`, `replacements` | Ordered exact replacements inside an existing file |

| Argument | Applies to | Purpose |
| --- | --- | --- |
| `changes` | Tool call | Ordered list of 1–64 file changes |
| `path` | Both kinds | File path within the workspace |
| `content` | `write` | Complete UTF-8 text to write |
| `replacements` | `edit` | Ordered entries with `old_text` and `new_text`; an empty `new_text` deletes the match |
| `expected_hash` | Both kinds | Optional 12-character SHA-256 prefix of the file before the batch |

#### Rules

- **One entry per file.** Two entries that resolve to the same real path are rejected,
  including aliases like `a.py` and `./a.py`.
- **`old_text` must occur exactly once** at the moment that replacement runs. Each
  replacement sees the result of the previous one, in order, and must carry enough
  context to be unique. Line-number prefixes from `read_file` output must be removed.
- **Text only.** The target and result must decode as UTF-8 and pass the binary-content check
  (the first 8 KiB must be free of NUL bytes).
- **`expected_hash` is optional.** When given, it is checked against the file *before any
  edit* in the batch. Compute it as the first 12 hexadecimal characters of the file
  bytes' SHA-256 digest.
- **At most 64 files** per call.

#### Preflight, then write

The whole batch is validated before anything is written:

1. Resolve every path and reject one that escapes the workspace, or that lands in a
   protected location: `.git`, `.hrns`, `.pydantic-ai-harness`, `.env*`, `secrets*`,
   `*.pem`, `*.key`. (The same policy the terminal client applies when it writes.)
2. Check duplicates, `expected_hash`, binary/UTF-8, and every replacement match.

A preflight failure leaves every file intact and raises a retryable tool error.
The toolset allows five consecutive retries.

Writes run sequentially. Each file is re-read immediately before writing to detect
concurrent changes. If a write fails, earlier writes remain applied and later files
are reported as `not attempted`:

```text
Batch incomplete: 1 of 3 files updated.
Updated src/app/settings.py (+1 -1, hash 4205c4809ab1)
src/app/main.py: failed — [Errno 13] Permission denied
src/app/other.py: not attempted
Read affected files before retrying; do not replay the whole batch blindly.
```

A successful call reports one line per file, with the line delta and the new hash:

```text
Created src/app/main.py (+12 -0, hash 8f0c2ad91b13)
Updated src/app/settings.py (+1 -1, hash 4205c4809ab1)
```

Writes go through `ctx.workspace.write_bytes`, so the workspace backend still decides
where bytes actually land.

#### Approval

Under the default approval policy, one batch produces one `session/request_permission`.
The decision covers every file in the batch.

The capability itself is `hrns.server.capabilities.changes.ApplyChanges` if you compose your own
coder; `Coder` is the supported way to get it.

## Todo

```python
from hrns.server.capabilities import Coder, Todo

agent = Agent(..., capabilities=[Coder(), Todo()])
```

`Todo` adds two tools:

| Tool | Returns | Notes |
| --- | --- | --- |
| `read_todos` | `tuple[TodoItem, ...]` | The current ordered list |
| `write_todos` | `tuple[TodoItem, ...]` | Replaces the complete list; an empty list clears it |

A `TodoItem` is `{content, priority: "high" | "medium" | "low", status: "pending" |
"in_progress" | "completed"}`. Every successful write emits a `TodoUpdated` capability
event (`namespace="hrns.todo"`, `name="updated"`) carrying the full snapshot, including an
empty one when the list is cleared.

State is a `TodoStore`. A standalone run gets a fresh one each turn unless you pass
`store=`; the ACP adapter binds its own per-session store, so todos survive prompts and
`session/load`, and each session receives its own ACP plan updates.

## Skills

```python
from pydantic_ai import Agent

from hrns.server.capabilities import Skills

agent = Agent(
    "anthropic:claude-sonnet-5-5",
    capabilities=[Skills((".agents/skills", "~/.agents/skills"))],
)
```

`Skills` provides a deferred catalog and bundled-file tools. Each discovered `SKILL.md`
becomes a deferred capability the model loads with `load_capability`; the bundled-file
tools are `read_skill_resource` and `run_skill_script`. The host-facing API includes:

- `catalog()` / `metadata()` read the libraries outside a run, so an ACP adapter can
  advertise skills on `session/new`;
- `instructions(id)` renders the text a person's `$skill` reference stands for, with
  `${SKILL_DIR}` resolved and the bundled-file inventory appended;
- library precedence: explicit paths, project library, then user library; duplicate
  names keep the first definition and its bundled files.

By default, `build_agent` adds `Skills.from_settings(...)`, which reads
`<cwd>/.agents/skills` and `~/.agents/skills` when they exist. `[skills]` in the
configuration document adds libraries, narrows the selection, or turns the capability
off. Discovery, invocation, the ACP wire format, and the security notes are on the
[Skills](skills.md) page.

## Ask user

`AskUser` provides `ask_user_question` as a deferred tool. The ACP adapter delivers
questions to the connected client through form elicitation.

```python
from hrns.server.capabilities import AskUser

agent = Agent("anthropic:claude-sonnet-5-5", capabilities=[AskUser()])
```

`ask_user_question` takes one to ten questions. Each has a short `header`, the
`question` text, two to six `options`, and a `multi_select` flag. The capability
validates the shape, including printable strings and unique option labels.

Deferred mode means the tool raises `CallDeferred`, and `PydanticAIACPAgent`
answers the deferred call by sending `elicitation/create` when the client advertises
form elicitation. One question becomes one form property, plus a companion text
field for a typed answer. The schema uses standard ACP form properties. Clients render
it with their elicitation UI. The hrns TUI shows the form in a dock above the composer.
It folds each choice
field and its typed-answer companion into one vertical option list, with the custom
answer as the last row; the first question is focused when the dock appears. For a
multi-select the custom answer is an alternative: choosing or typing it clears the
picked labels, because an ACP answer cannot combine a custom answer with selections.
Multiple questions use tabs, plus a final Confirm tab that reviews the answers. Enter
advances to the next question, Tab and Shift+Tab move between them, and Enter on
Confirm submits.

The model receives `{header: [labels...]}` — a one-item list holding the custom
answer when the human typed one. A decline, a cancel, an empty form, or a client
without form support all return a plain string telling the model to proceed with a
reasonable choice and say which one it made. These outcomes are returned as tool results.

`ask_user_question` is in the read-only approval set; the elicitation form collects
the user decision directly.

For direct local interaction, pass `answerer=...` to answer questions in the server
process. Use the default deferred mode for ACP clients.

## Approval

```python
from hrns.server.approvals import READ_ONLY_TOOLS, require_approval

agent = Agent(..., capabilities=[Coder(), require_approval()])
```

`require_approval` is a `PrepareTools` capability: it rewrites every function tool whose
name is not in `read_only` with `kind="unapproved"`. The ACP adapter turns that kind into
`session/request_permission`, and its own `permission_policy` decides the options the
client sees.

The default read-only set is:

```python
READ_ONLY_TOOLS = frozenset(
    {
        "read_file",
        "list_files",
        "grep",
        "read_todos",
        "write_todos",
        "ask_user_question",
        "load_capability",
        "search_tools",
        "read_skill_resource",
    }
)
```

`load_capability`, `search_tools`, and `read_skill_resource` run under the read-only
policy. `run_skill_script` and `shell` require approval.

Pass a custom set to allow additional tools automatically:

```python
require_approval(read_only=READ_ONLY_TOOLS | {"apply_changes"})
```

`build_agent` installs `require_approval()` by default. Pass `approval=False` to leave
tool approval to the surrounding deployment.

## Default agent composition

`build_agent` composes, in order: `LocalWorkspace(cwd)`, `Coder()`, `Todo()`,
`AskUser()`, `Skills.from_settings(...)` (when a library exists), and
`require_approval()` (unless `approval=False`). `LocalWorkspace` roots the tools at
`cwd`; `Coder` contributes the harness capabilities plus `apply_changes`; `Skills`
contributes the deferred skill catalog and its bundled-file tools. Adding another
capability is a list entry in `hrns/server/agent.py`.
