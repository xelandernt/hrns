"""Session-update constructors used by the adapter.

ACP 1.x removed the ``acp.helpers`` module, so these are the constructors the adapter needs,
expressed directly against ``acp.schema``.
"""

from __future__ import annotations

from collections.abc import Sequence

from acp import schema

ToolCallContent = (
    schema.ContentToolCallContent | schema.FileEditToolCallContent | schema.TerminalToolCallContent
)
ContentBlock = (
    schema.TextContentBlock
    | schema.ImageContentBlock
    | schema.AudioContentBlock
    | schema.ResourceContentBlock
    | schema.EmbeddedResourceContentBlock
)


def text_block(text: str) -> schema.TextContentBlock:
    return schema.TextContentBlock(type="text", text=text)


def update_user_message(content: ContentBlock) -> schema.UserMessageChunk:
    return schema.UserMessageChunk(session_update="user_message_chunk", content=content)


def update_agent_message_text(text: str) -> schema.AgentMessageChunk:
    return schema.AgentMessageChunk(session_update="agent_message_chunk", content=text_block(text))


def update_agent_thought_text(text: str) -> schema.AgentThoughtChunk:
    return schema.AgentThoughtChunk(session_update="agent_thought_chunk", content=text_block(text))


def start_tool_call(
    tool_call_id: str,
    title: str,
    *,
    kind: schema.ToolKind | None = None,
    status: schema.ToolCallStatus | None = None,
    content: Sequence[ToolCallContent] | None = None,
    locations: Sequence[schema.ToolCallLocation] | None = None,
    raw_input: object | None = None,
    raw_output: object | None = None,
) -> schema.ToolCallStart:
    return schema.ToolCallStart(
        session_update="tool_call",
        tool_call_id=tool_call_id,
        title=title,
        kind=kind,
        status=status,
        content=list(content) if content is not None else None,
        locations=list(locations) if locations is not None else None,
        raw_input=raw_input,
        raw_output=raw_output,
    )


def update_tool_call(
    tool_call_id: str,
    *,
    title: str | None = None,
    kind: schema.ToolKind | None = None,
    status: schema.ToolCallStatus | None = None,
    content: Sequence[ToolCallContent] | None = None,
    locations: Sequence[schema.ToolCallLocation] | None = None,
    raw_input: object | None = None,
    raw_output: object | None = None,
) -> schema.ToolCallProgress:
    return schema.ToolCallProgress(
        session_update="tool_call_update",
        tool_call_id=tool_call_id,
        title=title,
        kind=kind,
        status=status,
        content=list(content) if content is not None else None,
        locations=list(locations) if locations is not None else None,
        raw_input=raw_input,
        raw_output=raw_output,
    )


def tool_diff_content(
    path: str, new_text: str, old_text: str | None = None
) -> schema.FileEditToolCallContent:
    return schema.FileEditToolCallContent(
        type="diff", path=path, new_text=new_text, old_text=old_text
    )
