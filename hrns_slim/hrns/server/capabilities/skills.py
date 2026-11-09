"""Agent Skills: deferred capabilities from local libraries, with a host-facing catalog.

The agent side is [pydantic-ai-skills](https://github.com/DougTrajano/pydantic-ai-skills):
each ``SKILL.md`` package becomes a deferred capability the model loads with
``load_capability``, and the bundled-file tools come from the same package. This
capability adds what a protocol adapter needs and the library does not provide:

* **A host-facing catalog.** ``metadata()`` reads the libraries outside a run, so an
  ACP adapter can advertise skills on ``session/new`` without waiting for a prompt.
* **User-invoked instructions.** ``instructions()`` renders the text a person's
  ``$skill`` reference stands for: the body, ``${SKILL_DIR}`` resolved, and the
  bundled-file inventory the model needs to call the file tools.
* **Project-wins precedence.** Two libraries may define the same skill name; the first
  library wins, so the working directory's skill overrides the user-wide one.

The libraries are fixed when the capability is built, matching ``SkillsCapability``: it
resolves relative paths against the process directory once, rather than per session.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from pydantic_ai.agent import AbstractAgent
from pydantic_ai.capabilities import AbstractCapability, Capability, CombinedCapability
from pydantic_ai.output import OutputDataT
from pydantic_ai.tools import AgentDepsT, RunContext
from pydantic_ai.workspaces import LocalWorkspaceBackend
from pydantic_ai_harness import Skills as _HarnessSkills
from pydantic_ai_harness.skills import SkillCatalog, SkillDefinition
from pydantic_ai_skills import Skill, SkillsCapability, SkillScriptExecutor
from pydantic_ai_skills.packages import SkillPackage, index_libraries

from hrns.shared.config.settings import SkillsSettings

__all__ = ["SkillInfo", "Skills", "bind_invoked_skills", "find_skills"]

#: The project library, relative to the workspace root.
PROJECT_LIBRARY = Path(".agents") / "skills"

#: The user-wide library, relative to the home directory.
USER_LIBRARY = Path(".agents") / "skills"

#: Placeholders a published skill may use for its own directory.
_SKILL_DIR_PLACEHOLDERS = ("${SKILL_DIR}", "${CLAUDE_SKILL_DIR}")

#: Most bundled files listed in a skill's instructions.
_MAX_LISTED_FILES = 50

#: Skills a person invoked for this run; bound by the ACP adapter around a turn.
_invoked_skills: ContextVar[frozenset[str]] = ContextVar("hrns_invoked_skills", default=frozenset())


@contextmanager
def bind_invoked_skills(skill_ids: Iterable[str]) -> Iterator[None]:
    """Make the given skills always-on for the next run.

    A person invoking ``$name`` expects the instructions to be in force without the
    model spending a turn on ``load_capability``. The adapter binds the session's
    invoked ids around the run; :meth:`Skills.for_run` turns those leaves from
    deferred into always-on, which both injects the instructions and satisfies the
    bundled-file ``require_loaded`` gate (`active_capability_ids`).
    """
    token = _invoked_skills.set(frozenset(skill_ids))
    try:
        yield
    finally:
        _invoked_skills.reset(token)


@dataclass(frozen=True, kw_only=True)
class SkillInfo:
    """One skill as a host needs it: identity, copy for a picker, and invocation rules."""

    id: str
    """Capability id the model loads, and the name a person writes after ``$``."""

    name: str
    """Display name. Equal to ``id`` for Agent Skills discovered on disk."""

    description: str
    """The description the model sees in its deferred-capability catalog."""

    model_invocable: bool
    """Whether the model may load it (``disable-model-invocation`` reverses this)."""

    user_invocable: bool
    """Whether a host should offer it to a person (``user-invocable: false`` reverses this)."""

    path: str
    """Absolute path of the ``SKILL.md`` it came from; empty for a programmatic skill."""

    def to_meta(self) -> dict[str, object]:
        """The JSON object an ACP ``_meta`` payload carries for this skill."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "modelInvocable": self.model_invocable,
            "userInvocable": self.user_invocable,
            "path": self.path,
        }


class Skills(SkillsCapability[AgentDepsT]):
    """Agent Skills from local libraries, plus the catalog a protocol adapter advertises."""

    libraries: tuple[Path, ...]
    """Absolute library paths, highest precedence first."""

    cwd: Path
    """Working directory relative library paths were resolved against."""

    def __init__(
        self,
        libraries: Sequence[str | Path] = (),
        *,
        cwd: str | Path = ".",
        include: frozenset[str] | None = None,
        exclude: frozenset[str] | None = None,
        skills: Sequence[Skill] = (),
        script_executor: SkillScriptExecutor | None = None,
        id: str | None = "hrns.skills",
    ) -> None:
        if not libraries and not skills:
            raise ValueError("Skills needs at least one library directory or programmatic skill.")
        self.libraries = tuple(Path(path) for path in libraries)
        self.cwd = Path(cwd)
        self._catalog: SkillCatalog | None = None
        self._programmatic = tuple(skills)
        super().__init__(
            self.libraries,
            skills=self._programmatic,
            include=include,
            exclude=exclude,
            script_executor=script_executor,
            id=id,
        )
        self._adopt_first_package()

    async def for_run(self, ctx: RunContext[AgentDepsT]) -> AbstractCapability[AgentDepsT]:
        """Mark the skills a person invoked as active for this run.

        ``SkillsCapability.for_run`` decomposes harness's per-skill leaves. Replacing a
        leaf with ``defer_loading=False`` counts it as active, so the bundled-file tools
        pass their ``require_loaded`` gate, and drops it from the model's deferred
        catalog. The instructions themselves travel in the prompt: providers such as
        DeepSeek reject an assistant message that carries tool calls and no
        ``reasoning_content``, so a synthetic ``load_capability`` pair is not a portable
        way to preload a skill.
        """
        resolved = await super().for_run(ctx)
        invoked = _invoked_skills.get()
        if not invoked:
            return resolved
        replacement = resolved.visit_and_replace(lambda leaf: _activate(leaf, invoked))
        return replacement if replacement is not None else resolved

    # ------------------------------------------------------------------ sources

    @classmethod
    def default(cls, cwd: str | Path = ".") -> Skills[AgentDepsT] | None:
        """The capability with no configuration, or ``None`` when no library exists."""
        return cls.from_settings(SkillsSettings(), cwd=cwd)

    @classmethod
    def from_settings(
        cls, settings: SkillsSettings, cwd: str | Path = "."
    ) -> Skills[AgentDepsT] | None:
        """Build from ``[skills]``, or return ``None`` when it is disabled or empty.

        Explicit ``paths`` come first so they win a name collision; the project library
        comes next; the user-wide library last. A configured path that does not exist is
        an error: a typo in ``hrns.toml`` should not look like an empty skill set.
        """
        if not settings.enabled:
            return None
        base = Path(cwd).expanduser().absolute()

        explicit: list[Path] = []
        for raw in settings.paths:
            path = Path(raw).expanduser()
            if not path.is_absolute():
                path = base / path
            path = path.resolve()
            if not path.is_dir():
                raise ValueError(f"Skill library does not exist or is not a directory: {path}")
            explicit.append(path)

        discovered = (base / PROJECT_LIBRARY, Path.home() / USER_LIBRARY)
        libraries = tuple(dict.fromkeys([*explicit, *(p for p in discovered if p.is_dir())]))
        if not libraries:
            return None
        return cls(
            libraries,
            cwd=base,
            include=frozenset(settings.include) or None,
            exclude=frozenset(settings.exclude),
        )

    # ------------------------------------------------------------------ catalog

    async def catalog(self) -> SkillCatalog:
        """Read the libraries once, outside a run, and cache the result.

        ``pydantic-ai-skills`` defers its own discovery to the start of each run, so
        this is the only way a protocol adapter can answer ``session/new`` with the
        skills a person may invoke.
        """
        if self._catalog is None:
            if self.libraries:
                self._catalog = await self._loader().load(LocalWorkspaceBackend(self.cwd))
            else:
                self._catalog = SkillCatalog(skills=(), skipped=())
        return self._catalog

    async def metadata(self) -> tuple[SkillInfo, ...]:
        """The catalog as host-facing records, sorted by skill id.

        A programmatic skill shadows a discovered one with the same name, as it does on
        the agent side, so the catalog and the model's view never disagree.
        """
        catalog = await self.catalog()
        records = {
            skill.name: SkillInfo(
                id=skill.name,
                name=skill.name,
                description=skill.description,
                model_invocable=skill.model_invocable,
                user_invocable=skill.user_invocable,
                path=skill.path,
            )
            for skill in catalog.skills
        }
        for skill in self._programmatic:
            records[skill.name] = SkillInfo(
                id=skill.name,
                name=skill.name,
                description=skill.description,
                model_invocable=True,
                user_invocable=True,
                path="",
            )
        return tuple(records[name] for name in sorted(records))

    async def instructions(self, skill_id: str) -> str:
        """Render a skill's instructions exactly as a ``load_capability`` call would.

        Harness renders the heading, the body, and the skill's directory;
        ``pydantic-ai-skills`` resolves ``${SKILL_DIR}`` and lists the bundled files. A
        host-invoked skill needs the same text in one step, without a model round trip.
        """
        programmatic = next((item for item in self._programmatic if item.name == skill_id), None)
        if programmatic is not None:
            text = f"# Skill: {programmatic.name}\n\n{programmatic.content}".rstrip()
        else:
            catalog = await self.catalog()
            definition = next((item for item in catalog.skills if item.name == skill_id), None)
            if definition is None:
                raise KeyError(skill_id)
            text = _resolve_placeholders(definition.render(), definition)
        inventory = _bundled_files(self.packages.get(skill_id))
        return f"{text}\n\n{inventory}" if inventory is not None else text

    # ------------------------------------------------------------------ internals

    def _adopt_first_package(self) -> None:
        """Give a shadowed skill the bundled files of the skill that won.

        ``pydantic-ai-skills`` indexes the libraries in order and keeps the last
        package for a duplicate name, while harness keeps the first skill. Without
        this, ``read_skill_resource`` would read the losing library's files while the
        model saw the winning library's instructions. The toolset holds this same
        dict, so updating it in place is enough.
        """
        if len(self.libraries) < 2:
            return
        merged: dict[str, SkillPackage] = {}
        for library in reversed(self.libraries):
            merged.update(index_libraries([library]))
        for name, package in tuple(self._packages.items()):
            replacement = merged.get(name)
            if package.directory is not None and replacement is not None:
                self._packages[name] = replacement

    def _loader(self) -> _HarnessSkills[AgentDepsT]:
        """A harness ``Skills`` over the same libraries, used for host-side reads."""
        if self.include is not None:
            return _HarnessSkills(
                self.libraries,
                include=sorted(self.include),
                duplicate_names="keep_first",
            )
        return _HarnessSkills(
            self.libraries,
            exclude=sorted(self.exclude),
            duplicate_names="keep_first",
        )

    def _build_harness_skills(
        self,
        libraries: Sequence[str | Path],
        directory_names: frozenset[str],
        shadowed: frozenset[str],
    ) -> _HarnessSkills[AgentDepsT] | None:
        """Build the harness catalog with project-wins duplicates.

        A near-copy of ``SkillsCapability``'s own method: it does not expose
        ``duplicate_names``, and the default ``'error'`` turns the ordinary case of a
        project skill overriding a user-wide one into a failed run. Keep this in sync
        with the pinned ``pydantic-ai-skills``.
        """
        if not libraries:
            return None
        workspace = LocalWorkspaceBackend(self.cwd)
        if self.include is not None:
            return _HarnessSkills(
                libraries,
                include=sorted((self.include & directory_names) - shadowed),
                duplicate_names="keep_first",
                workspace=workspace,
            )
        return _HarnessSkills(
            libraries,
            exclude=sorted((self.exclude | shadowed) & directory_names),
            duplicate_names="keep_first",
            workspace=workspace,
        )


def find_skills(agent: AbstractAgent[AgentDepsT, OutputDataT]) -> Skills[AgentDepsT] | None:
    """The first :class:`Skills` capability on an agent, if it has one.

    Found without the ACP adapter knowing how the agent was composed, so a custom
    ``build_agent`` call still gets the catalog by adding the capability.
    """
    pending: list[AbstractCapability[AgentDepsT]] = [agent.root_capability]
    while pending:
        capability = pending.pop()
        if isinstance(capability, Skills):
            return capability
        if isinstance(capability, CombinedCapability):
            pending.extend(capability.capabilities)
    return None


def _activate(
    capability: AbstractCapability[AgentDepsT], invoked: frozenset[str]
) -> AbstractCapability[AgentDepsT]:
    """A deferred skill leaf, rebuilt as always-on when a person invoked it."""
    if capability.defer_loading is not True or capability.id not in invoked:
        return capability
    return Capability[AgentDepsT](
        id=capability.id,
        description=capability.get_description(),
        instructions=capability.get_instructions(),
        defer_loading=False,
    )


def _resolve_placeholders(text: str, definition: SkillDefinition) -> str:
    """Substitute the skill-directory placeholders harness leaves in place."""
    for placeholder in _SKILL_DIR_PLACEHOLDERS:
        text = text.replace(placeholder, definition.directory)
    return text


def _bundled_files(package: SkillPackage | None) -> str | None:
    """The inventory ``pydantic-ai-skills`` appends to a skill's instructions."""
    if package is None:
        return None
    blocks: list[str] = []
    if package.resources:
        names = sorted(resource.name for resource in package.resources)
        blocks.append(
            "Read with `read_skill_resource`, using these exact `resource_name` values:"
            f"\n\n{_bullets(names)}"
        )
    if package.scripts:
        names = sorted(script.name for script in package.scripts)
        blocks.append(
            "Run with `run_skill_script`, using these exact `script_name` values:"
            f"\n\n{_bullets(names)}"
        )
    if not blocks:
        return None
    return "## Bundled files\n\n" + "\n\n".join(blocks)


def _bullets(names: list[str]) -> str:
    listed = [f"- `{name}`" for name in names[:_MAX_LISTED_FILES]]
    remaining = len(names) - len(listed)
    if remaining > 0:
        listed.append(f"- ...and {remaining} more")
    return "\n".join(listed)
