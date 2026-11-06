# Capabilities

`build_agent` builds a Pydantic AI `Agent` out of [capabilities](https://ai.pydantic.dev/capabilities/).
This page documents the capabilities **this repository authors**. Everything else on the
agent is a `pydantic-ai` or `pydantic-ai-harness` capability; those are documented by their
own projects and are not repeated here.

| Section                                                 | Import                                   | What it adds                                                  |
| ------------------------------------------------------- | ---------------------------------------- | ------------------------------------------------------------- |
| [Coder](#coder)                                         | `hrns.capabilities.Coder`                | The harness coder, plus the `apply_changes` batch tool        |
| [`apply_changes`](#apply_changes)                       | `hrns.capabilities.changes.ApplyChanges` | One approved batch of many writes and edits                   |
| [Todo](#todo)                                           | `hrns.capabilities.Todo`                 | `read_todos` / `write_todos`, and a typed `TodoUpdated` event |
| [Ask user](#ask-user)                                   | `hrns.capabilities.AskUser`              | `ask_user_question`, rendered as an ACP form elicitation       |
| [Approval](#approval)                                   | `hrns.app.approvals.require_approval`    | Marks every non-read-only tool `unapproved`                   |
| [Where the rest comes from](#where-the-rest-comes-from) | `hrns.app.agent.build_agent`             | The default composition of all of the above                   |

## Coder

```python
from pydantic_ai import Agent
from pydantic_ai.capabilities import LocalWorkspace

from hrns.capabilities import Coder

agent = Agent(
    "anthropic:claude-sonnet-5-5",
    capabilities=[LocalWorkspace("."), Coder()],
)
```

`Coder` subclasses `pydantic_ai_harness.coder.Coder`. The constructor, its keyword
arguments (`instructions`, `unrestricted_filesystem`, `repo_context`, `sub_agents`,
`agent_folders`, `caching`), and every harness capability it installs are unchanged:
`read_file`, `write_file`, `edit_file`, `list_files`, `grep`, `shell`, sub-agents,
repo context, and context management still come from the harness. See
[pydantic-ai-harness](https://github.com/pydantic/pydantic-ai-harness) for those.
The positional `workspace` argument is accepted but ignored with a warning — attach
`LocalWorkspace(path)` next to `Coder()` instead.

The one difference is an extra capability appended in `__post_init__`. That hook runs
inside `CombinedCapability.__init__`, before the composition is flattened, so
`ApplyChanges` becomes a sibling of the harness capabilities rather than a wrapper around
them: tool ordering, instructions, and per-run rebinding behave as if it had been passed
to the harness constructor.

### `apply_changes`

The harness filesystem applies one change per call, so editing five files costs five
approvals and five round trips. `apply_changes` takes a whole batch in one call; its
arguments look like this:

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

Both shapes accept an optional `expected_hash`. A replacement is
`{"old_text": ..., "new_text": ...}`; `new_text` may be empty to delete text.

#### Rules

- **One entry per file.** Two entries that resolve to the same real path are rejected,
  including aliases like `a.py` and `./a.py`.
- **`old_text` must occur exactly once** at the moment that replacement runs. Each
  replacement sees the result of the previous one, in order, and must carry enough
  context to be unique. Line-number prefixes from `read_file` output must be removed.
- **Text only.** The target must decode as UTF-8 and must not contain a NUL byte in the
  first 8 KiB; the result must stay text too.
- **`expected_hash` is optional.** When given, it is checked against the file *before any
  edit* in the batch. It is the same 12-character `sha256` prefix the filesystem tools
  report.
- **At most 64 files** per call.

#### Preflight, then write

The whole batch is validated before anything is written:

1. Resolve every path and reject one that escapes the workspace, or that lands in a
   protected location: `.git`, `.hrns`, `.pydantic-ai-harness`, `.env*`, `secrets*`,
   `*.pem`, `*.key`. (The same policy the terminal client applies when it writes.)
2. Check duplicates, `expected_hash`, binary/UTF-8, and every replacement match.

A preflight failure raises a retryable tool error that says **no files were changed**, and
the model may correct the batch. The toolset allows five consecutive retries.

Writing is *not* atomic. Each file is re-read immediately before it is written, so a file
that changed during preflight is refused; if a write still fails, the files already
written stay written, the untouched ones are reported `not attempted`, and the summary
does not claim otherwise:

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

`apply_changes` is not in `READ_ONLY_TOOLS`, so under the default approval policy one
batch becomes **one** `session/request_permission` rather than one per file. The approval
covers everything in the batch: a client sees a single, all-or-nothing decision.

The capability itself is `hrns.capabilities.changes.ApplyChanges` if you compose your own
coder; `Coder` is the supported way to get it.

## Todo

```python
from hrns.capabilities import Coder, Todo

agent = Agent(..., capabilities=[Coder(), Todo()])
```

`Todo` adds two tools and no reminder prompt:

| Tool | Returns | Notes |
| --- | --- | --- |
| `read_todos` | `tuple[TodoItem, ...]` | The current ordered list |
| `write_todos` | `tuple[TodoItem, ...]` | Replaces the complete list; an empty list clears it |

A `TodoItem` is `{content, priority: "high" | "medium" | "low", status: "pending" |
"in_progress" | "completed"}`. Every successful write emits a `TodoUpdated` capability
event (`namespace="hrns.todo"`, `name="updated"`) carrying the full snapshot, including an
empty one — so a consumer can distinguish "cleared" from "no news" without diffing.

State is a `TodoStore`. A standalone run gets a fresh one each turn unless you pass
`store=`; the ACP adapter binds its own per-session store, so todos survive prompts and
`session/load`, and each ACP session gets ACP plan updates instead of shared state.

## Ask user

`hrns.capabilities.AskUser` subclasses the harness capability and changes one
default: the tool is deferred instead of answered in-process. That is the only mode
that works when the human sits behind a protocol.

```python
from hrns.capabilities import AskUser

agent = Agent("anthropic:claude-sonnet-5-5", capabilities=[AskUser()])
```

`ask_user_question` takes one to ten questions. Each has a short `header` (the tab
label, unique within one call), the `question` text, two to six `options`, and a
`multi_select` flag. The harness validates the shape, including printable strings
and unique option labels.

Deferred mode means the tool raises `CallDeferred`, and `PydanticAIACPAgent`
answers the deferred call by sending `elicitation/create` when the client advertises
form elicitation. One question becomes one form property, plus a companion text
field for a typed answer. The schema is standard ACP; a single `hrns.ask_user` key
in `_meta` marks it so the hrns TUI renders tabs and a custom-answer field, while
any other client still gets a usable form.

The model receives `{header: [labels...]}` — a one-item list holding the custom
answer when the human typed one. A decline, a cancel, an empty form, or a client
without form support all return a plain string telling the model to proceed with a
reasonable choice and say which one it made. None of them is a tool error.

`ask_user_question` is read-only: the question is the interaction, so it is never
wrapped in a second permission prompt.

Pass `answerer=...` only for a run with a local human, such as a CLI or a test.
Doing that while mounted over ACP would answer on the server and the client would
never see the question.

## Approval

```python
from hrns.app.approvals import READ_ONLY_TOOLS, require_approval

agent = Agent(..., capabilities=[Coder(), require_approval()])
```

`require_approval` is a `PrepareTools` capability: it rewrites every function tool whose
name is not in `read_only` with `kind="unapproved"`. The ACP adapter turns that kind into
`session/request_permission`, and its own `permission_policy` decides the options the
client sees.

The default read-only set is:

```python
READ_ONLY_TOOLS = frozenset(
    {"read_file", "list_files", "grep", "read_todos", "write_todos", "ask_user_question"}
)
```

Pass your own set to change it — this is how you allow additional tools without a prompt:

```python
require_approval(read_only=READ_ONLY_TOOLS | {"apply_changes"})
```

`build_agent` installs `require_approval()` by default. Pass `approval=False` to leave
every tool unapproved-free, which is only appropriate for a sandboxed deployment.

## Where the rest comes from

`build_agent` composes, in order: `LocalWorkspace(cwd)`, `Coder()`, `Todo()`,
`AskUser()`, and `require_approval()` (unless `approval=False`).
`LocalWorkspace` roots the tools at `cwd`; `Coder` contributes the harness capabilities
plus `apply_changes`. Adding another capability is a list entry in `hrns/app/agent.py` —
there is no registry to update.
