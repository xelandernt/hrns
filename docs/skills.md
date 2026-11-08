# Skills

Skills are folders of instructions — and, optionally, reference files and scripts — that
teach the agent a specialized workflow. The model starts out seeing only each skill's
name and description. When a task needs one, it loads that skill's instructions; a
reference file or a script is read or run only if the instructions call for it. This is
*progressive disclosure*: a skill library can grow without every skill paying for space
in the prompt.

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
2. Check the failure paths, not only the happy path.
3. Use `references/checklist.md` when the change touches a public API.

Run `scripts/summarize.sh` with the changed paths to produce a short summary.
```

`name` and `description` are what the runtime acts on. `description` is what the model
sees in its catalog, so write it as a sentence about *when* to use the skill. Other
frontmatter is accepted; two fields change invocation:

| Frontmatter | Effect |
| --- | --- |
| `disable-model-invocation: true` | The skill is left out of the model's catalog; a person can still invoke it with `$name` |
| `user-invocable: false` | The skill is not offered to a person (no `$` completion, and the server rejects a reference); the model may still load it |

## Discovery

With no configuration, the built-in agent reads these libraries:

| Scope | Path |
| --- | --- |
| Project | `<workspace>/.agents/skills` |
| User | `~/.agents/skills` |

A library is the *parent* of skill directories, not a skill itself. When the same skill
name appears in more than one library, the first library wins — project over user,
explicit paths over project — including its bundled files. A missing default library is
ignored; a configured path that does not exist is a configuration error.

`[skills] paths` adds libraries ahead of the defaults, and `include` / `exclude` narrow
the catalog. See [Configuration](#configuration).

The catalog is fixed when the session is created. A skill added to disk afterwards
appears after the client opens a new session or reloads the current one.

## Model invocation

Every discovered skill becomes a deferred capability. The model sees `id` and
`description` in its deferred-capability catalog and calls `load_capability` to pull in
the instructions. Pydantic AI then marks the capability loaded, and the skill's bundled
files become reachable with:

| Tool | Purpose |
| --- | --- |
| `read_skill_resource(skill_name, resource_name)` | Read a file such as `references/checklist.md` |
| `run_skill_script(skill_name, script_name, args)` | Run a bundled script |

Both tools refuse a skill that has not been loaded, so a skill's files stay behind the
same boundary as its instructions. `${SKILL_DIR}` and `${CLAUDE_SKILL_DIR}` in the
instructions are replaced with the skill's real directory, and the instructions end with
an inventory of the bundled files so the model knows the exact names the tools expect.

`read_skill_resource` is read-only. `run_skill_script` is not, so under the default
approval policy it becomes one `session/request_permission` per call.

## User invocation

Type `$` in the composer to pick a skill — a bare `$` lists every skill alphabetically,
typing narrows the list — or run `/skills` to list them. On submit, the client sends the
skill's id with the prompt; it never sends the body. The server resolves the id, prepends
the rendered instructions to that turn as a `<skill_content name="…">` block, and marks
the skill active for the session. The transcript shows a `Loaded skill: <name>` marker.

This is the same shape OpenCode uses, and it is provider-safe: the instructions arrive as
user content, never as a fabricated assistant turn. Providers in thinking mode — DeepSeek,
for instance — reject an assistant message that carries tool calls and no
`reasoning_content`, so a synthetic `load_capability` exchange cannot be used to preload a
skill. Marking the capability active is what opens the bundled-file gate
(`require_loaded`), and it removes the skill from the model's deferred catalog so it is
not offered for loading twice. Activation is remembered per session and restored by
`session/load`.

The reference travels as standard ACP metadata — there is no `_hrns/*` extension:

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

A standard ACP agent returns no `_meta`, so the terminal simply offers no `$` completion
or `/skills` for that connection. Headless runs (`hrns run "… $code-review"`) use the
same reference and resolve it the same way.

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
| `paths` | list of paths | `[]` | Extra libraries, read before the defaults. Relative paths resolve against the working directory; `~` is expanded. A path that does not exist is an error |
| `include` | list of strings | `[]` | Exact skill ids to expose. Empty exposes everything discovered |
| `exclude` | list of strings | `[]` | Exact skill ids to hide. Cannot be combined with `include` |

Precedence, highest first: `paths` in the order written, then `<workspace>/.agents/skills`,
then `~/.agents/skills`. Setting `enabled = false` removes the capability entirely: the
agent has no skill tools and the adapter advertises no catalog.

## Programmatic skills

A deployment can ship skills in Python instead of on disk — useful when the instructions
are generated, or when the app is the only source of truth. Pass them to the capability:

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

agent = Agent(capabilities=[Skills(programmatic=(RELEASE_NOTES,))])
```

Programmatic skills join the same catalog: the model can load them with
`load_capability`, and a person can invoke them with `$release-notes`. A programmatic
skill shadows a discovered one with the same name, matching the agent-side behavior.
`examples/fastapi_app.py` ships two of them.

## Security

Only install skills from sources you trust. A skill is instructions plus, usually, code:
a malicious one can direct the agent to call tools or run a script in ways that do not
match its stated purpose. A project-local library is controlled by whoever controls the
repository, exactly like the rest of the workspace.

The runtime does not sandbox scripts: `run_skill_script` runs on the host, subject to the
same approval policy as `shell`. If the libraries come from a source you do not fully
trust, compose your own agent with `Skills(..., script_executor=...)` and pass a
sandboxed executor; `[skills]` in the document cannot configure one.

## Limitations

- **Local libraries only.** Git, S3, and HTTP registries from `pydantic-ai-skills` are
  not wired into the configuration yet; see `TODO.md`.
- **Session snapshot.** The catalog is read when the session starts. Reopen the session
  to pick up a new skill.
- **Model-hidden skills and file tools.** A skill with `disable-model-invocation: true`
  has no capability, so `read_skill_resource` / `run_skill_script` refuse it even when a
  person invokes it. Its rendered instructions (with the absolute directory and
  bundled-file inventory) still join the prompt, so the ordinary filesystem tools can
  reach the files.

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
catalog and `$name` keep working without adapter changes.
