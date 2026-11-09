# Skills

Skills are folders of instructions — and, optionally, reference files and scripts — that
teach the agent a specialized workflow. The model starts out seeing only each skill's
name and description. When a task needs one, it loads that skill's instructions; a
reference file or a script is read or run only if the instructions call for it. This is
*progressive disclosure*: the prompt includes the instructions needed for the current task.

`hrns` reads the [Agent Skills](https://agentskills.io/specification) format and adds a
host-facing catalog, so a person can list skills and invoke one with `$name` as well as
letting the model discover it.

## Layout

A skill is a directory with a `SKILL.md` at its root:

```text
.agents/skills/
└── code-review/
    ├── SKILL.md
    ├── references/
    │   └── checklist.md
    └── scripts/
        └── summarize.sh
```

```markdown
---
name: code-review
description: Review a change for correctness, regressions, and missing tests
---

# Code review

1. Read the change and the tests that cover it.
2. Check successful execution and failure paths.
3. Use `references/checklist.md` when the change touches a public API.

Run `scripts/summarize.sh` with the changed paths to produce a short summary.
```

`name` and `description` are what the runtime acts on. `description` is what the model
sees in its catalog, so write it as a sentence about *when* to use the skill. Other
frontmatter is accepted; two fields change invocation:

| Frontmatter | Effect |
| --- | --- |
| `disable-model-invocation: true` | The skill is left out of the model's catalog; a person can still invoke it with `$name` |
| `user-invocable: false` | Reserve the skill for model invocation; user references receive `invalid_params` |

## Discovery

The built-in agent discovers these libraries by default:

| Scope | Path |
| --- | --- |
| Project | `<workspace>/.agents/skills` |
| User | `~/.agents/skills` |

A library contains skill directories. When the same skill
name appears in more than one library, the first library wins — project over user,
explicit paths over project — including its bundled files. A missing default library is
ignored; an explicit path must point to an existing directory.

`[skills] paths` adds libraries ahead of the defaults, and `include` / `exclude` narrow
the catalog. See [Configuration](#configuration).

The catalog is cached on the session's agent. Open a new session to discover newly
added skills. Loading a saved session after a server restart rebuilds its agent and catalog.

## Model invocation

Every discovered skill becomes a deferred capability. The model sees `id` and
`description` in its deferred-capability catalog and calls `load_capability` to pull in
the instructions. The runtime marks the capability loaded, and the skill's bundled
files become reachable with:

| Tool | Purpose |
| --- | --- |
| `read_skill_resource(skill_name, resource_name)` | Read a file such as `references/checklist.md` |
| `run_skill_script(skill_name, script_name, args)` | Run a bundled script |

Both tools require an active skill, keeping bundled-file access tied to instruction
loading. `${SKILL_DIR}` and `${CLAUDE_SKILL_DIR}` in the
instructions are replaced with the skill's real directory, and the instructions end with
an inventory of the bundled files so the model knows the exact names the tools expect.

`read_skill_resource` runs under the read-only policy. `run_skill_script` requires
one `session/request_permission` per call under the default approval policy.

## User invocation

Type `$` in the composer to pick a skill — a bare `$` lists every skill alphabetically,
typing narrows the list — or run `/skills` to list them. On submit, the client sends the
skill's id with the prompt. The server resolves the id, prepends
the rendered instructions to that turn as a `<skill_content name="…">` block, and marks
the skill active for the session. The transcript shows a `Loaded skill: <name>` marker.

Instructions arrive as user content. Activating the capability enables its bundled-file
tools and removes it from the deferred catalog for subsequent runs in the session.
Activation is remembered per session and restored by `session/load`.

The reference travels in ACP `_meta`:

```json
{
  "sessionId": "…",
  "prompt": [
    {
      "type": "text",
      "text": "review src/app.py $code-review",
      "_meta": {
        "hrns/skills": [
          {
            "id": "code-review",
            "mention": { "start": 18, "end": 30, "text": "$code-review" }
          }
        ]
      }
    }
  ]
}
```

The `mention` offsets are display information for the client; the server validates the
`id` against the session's catalog and ignores the rest. An unknown id, or one whose
skill is not user-invocable, is rejected with `invalid_params` before the turn starts.

The catalog itself arrives on `session/new` (and `session/load`) in the response's
`_meta`, which is the field ACP reserves for exactly this kind of out-of-band payload:

```json
{
  "sessionId": "…",
  "_meta": {
    "hrns/skills": [
      {
        "id": "code-review",
        "name": "code-review",
        "description": "Review a change for correctness, regressions, and missing tests",
        "modelInvocable": true,
        "userInvocable": true,
        "path": "/repo/.agents/skills/code-review/SKILL.md"
      }
    ]
  }
}
```

The terminal enables `$` completion and `/skills` when the connected agent advertises
the catalog. Headless runs (`hrns run "… $code-review"`) use the same reference format.

## Configuration

`[skills]` lives in the same document as the rest of the runtime settings.

```toml
[skills]
enabled = true
paths = ["~/shared/skills", "./team-skills"]
include = []
exclude = ["experimental-release-notes"]
```

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | Expose skills to the agent and to host-side invocation |
| `paths` | list of paths | `[]` | Extra libraries, read before the defaults. Relative paths resolve against the working directory; `~` is expanded. Each explicit path must be an existing directory |
| `include` | list of strings | `[]` | Exact skill ids to expose. Empty exposes everything discovered |
| `exclude` | list of strings | `[]` | Exact skill ids to hide. Use either `include` or `exclude` |

Precedence, highest first: `paths` in the order written, then `<workspace>/.agents/skills`,
then `~/.agents/skills`. Set `enabled = false` to disable skills for the agent and ACP catalog.

## Programmatic skills

Define application-owned or generated instructions in Python and pass them with `skills=`.
See the [programmatic skills documentation](https://dougtrajano.github.io/pydantic-ai-skills/programmatic-skills/)
for `Skill`, resource, and script definitions:

```python
from pydantic_ai import Agent
from pydantic_ai_skills import Skill, SkillResource

from hrns.server.capabilities import Skills

RELEASE_NOTES = Skill(
    name="release-notes",
    description="Turn merged changes into release notes.",
    content="Group changes under New, Improved, and Fixed. Write for the user.",
    resources=[SkillResource(name="style-guide", content="- One sentence per change.")],
)

agent = Agent(
    "anthropic:claude-sonnet-5-5",
    capabilities=[Skills(skills=(RELEASE_NOTES,))],
)
```

Programmatic skills join the same catalog: the model can load them with
`load_capability`, and a person can invoke them with `$release-notes`. A programmatic
skill shadows a discovered one with the same name, matching the agent-side behavior.
`examples/fastapi_app.py` ships two of them.

## Capability arguments

`Skills(libraries=..., skills=...)` accepts local libraries and Python definitions.

| Argument | Type | Default | Purpose |
| --- | --- | --- | --- |
| `libraries` | `Sequence[str \| Path]` | `()` | Library directories, highest precedence first |
| `cwd` | `str \| Path` | `"."` | Workspace used to read the host-facing catalog |
| `skills` | `Sequence[Skill]` | `()` | Python-defined skills; these take precedence over disk definitions with the same name |
| `include` | `frozenset[str] \| None` | `None` | Exact names to expose |
| `exclude` | `frozenset[str] \| None` | `None` | Exact names to omit; use either `include` or `exclude` |
| `script_executor` | `SkillScriptExecutor \| None` | `None` | Executor for bundled scripts; the default runs on the host |
| `id` | `str \| None` | `"hrns.skills"` | Capability identifier for the bundled-file tools |

Provide at least one library or Python skill. For configured discovery and path resolution,
use `Skills.from_settings(settings, cwd=...)` or `Skills.default(cwd)`.

## Execution and catalog scope

Install skills from trusted sources: their instructions can direct tool calls and script
execution. Project-local skills share the repository's trust boundary.
`run_skill_script` executes on the host under the same approval policy as `shell`.
For isolated execution, compose an agent with `Skills(..., script_executor=...)` and a
[sandbox executor](https://dougtrajano.github.io/pydantic-ai-skills/sandbox/).
The executor is configured through Python.

Configuration discovers local libraries. Open a new session to discover newly added skills. A skill with `disable-model-invocation: true`
contributes rendered instructions when invoked by a user. Its bundled files are accessible
through ordinary filesystem tools using the absolute paths in those instructions;
the dedicated bundled-file tools require a model-loadable capability.

## Where it lives

| Piece | Module |
| --- | --- |
| The capability, catalog, and renderer | `hrns.server.capabilities.skills.Skills` |
| The default composition | `hrns.server.agent.build_agent` |
| The ACP catalog and prompt reference | `hrns.server.acp.adapter` |
| The client-side `$name` scanner | `hrns.client.skills` |
| The terminal workflow | `hrns.client.tui.chat.prompts.PromptWorkflow` |

`Skills` is a capability in the same sense as [`Coder` and `Todo`](capabilities.md): if
you build your own agent, add it to the capability list and the adapter finds it, so the
adapter advertises the catalog and supports `$name` invocation.
