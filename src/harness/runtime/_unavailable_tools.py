"""Report calls to remembered tools without restoring disabled capabilities."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from pydantic_ai import RunContext, ToolFailed
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset, WrapperToolset
from pydantic_ai.toolsets.abstract import ToolsetTool
from pydantic_core import SchemaValidator, core_schema

_UNAVAILABLE = "harness.unavailable_tool"


@dataclass
class UnavailableTools(AbstractCapability[Any]):
    """Keep old tool names callable only as failures when bindings are disabled.

    The original capability is never constructed or entered. Refusal tools accept any
    object arguments and are normally hidden from model requests. DeepSeek retains
    error-only declarations when no active tools remain, avoiding its raw DSML output
    bug when conversation history contains tool calls but the request omits tools.
    Active tools always take precedence. The session transcript supplies function-tool
    names, including after resume, without treating output tools as refusal tools.
    """

    disabled_bindings: tuple[str, ...]
    remembered_tools: frozenset[str]

    def get_wrapper_toolset(self, toolset: AbstractToolset[Any]) -> AbstractToolset[Any]:
        return _UnavailableToolset(toolset, self.disabled_bindings, self.remembered_tools)

    async def before_model_request(
        self, ctx: RunContext[Any], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        parameters = request_context.model_request_parameters
        visible_tools = [
            tool
            for tool in parameters.function_tools
            if not (tool.metadata or {}).get(_UNAVAILABLE)
        ]
        # DeepSeek can leak its internal DSML format instead of structured tool calls
        # when the last tool is removed from an existing conversation:
        # https://github.com/deepseek-ai/DeepSeek-V3/issues/1678
        # These declarations have no capability implementation behind them; calls
        # still raise ToolFailed and tell the model to explain the limitation.
        if (
            request_context.model.system == "deepseek"
            and not visible_tools
            and not parameters.output_tools
        ):
            visible_tools = parameters.function_tools
        return replace(
            request_context,
            model_request_parameters=replace(
                parameters,
                function_tools=visible_tools,
            ),
        )


@dataclass
class _UnavailableToolset(WrapperToolset[Any]):
    disabled_bindings: tuple[str, ...]
    remembered_tools: frozenset[str]

    async def get_tools(self, ctx: RunContext[Any]) -> dict[str, ToolsetTool[Any]]:
        tools = dict(await super().get_tools(ctx))
        for name in sorted(self.remembered_tools - tools.keys()):
            tools[name] = ToolsetTool(
                toolset=self,
                tool_def=ToolDefinition(
                    name=name,
                    description=(
                        "Unavailable: this tool is absent from the current capability "
                        "configuration. Calling it only returns an error. Do not call it; "
                        "explain the limitation to the user instead."
                    ),
                    metadata={_UNAVAILABLE: True},
                ),
                max_retries=0,
                args_validator=SchemaValidator(
                    core_schema.dict_schema(core_schema.str_schema(), core_schema.any_schema())
                ),
            )
        return tools

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[Any], tool: ToolsetTool[Any]
    ) -> Any:
        if (tool.tool_def.metadata or {}).get(_UNAVAILABLE):
            disabled = ", ".join(repr(binding) for binding in self.disabled_bindings)
            raise ToolFailed(
                f"Tool {name!r} is unavailable in the current capability configuration. "
                f"Disabled capability bindings: {disabled}. "
                "Do not call this tool again unless its capability is enabled. "
                "Use the available tools or explain this limitation to the user."
            )
        return await super().call_tool(name, tool_args, ctx, tool)
