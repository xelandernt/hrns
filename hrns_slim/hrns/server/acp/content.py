"""Translation between ACP content blocks and Pydantic AI user content."""

from __future__ import annotations

import base64
from collections.abc import Sequence

from acp import schema
from pydantic_ai.messages import BinaryContent, ImageUrl, UserContent

PromptContentBlock = (
    schema.TextContentBlock
    | schema.ImageContentBlock
    | schema.AudioContentBlock
    | schema.ResourceContentBlock
    | schema.EmbeddedResourceContentBlock
)

# Used when an embedded binary resource arrives without a declared media type; an empty
# media type would later raise when the content is formatted for a model request.
_DEFAULT_BINARY_MEDIA_TYPE = "application/octet-stream"


def prompt_blocks_to_user_content(blocks: Sequence[PromptContentBlock]) -> list[UserContent]:
    """Convert ACP prompt content blocks into Pydantic AI user content."""
    content: list[UserContent] = []
    for block in blocks:
        if isinstance(block, schema.TextContentBlock):
            content.append(block.text)
        elif isinstance(block, schema.ImageContentBlock):
            if not block.data and block.uri is not None:
                content.append(ImageUrl(url=block.uri))
            else:
                content.append(
                    BinaryContent(data=base64.b64decode(block.data), media_type=block.mime_type)
                )
        elif isinstance(block, schema.AudioContentBlock):
            content.append(
                BinaryContent(data=base64.b64decode(block.data), media_type=block.mime_type)
            )
        elif isinstance(block, schema.EmbeddedResourceContentBlock):
            resource = block.resource
            if isinstance(resource, schema.TextResourceContents):
                content.append(resource.text)
            else:
                media_type = resource.mime_type or _DEFAULT_BINARY_MEDIA_TYPE
                content.append(
                    BinaryContent(data=base64.b64decode(resource.blob), media_type=media_type)
                )
        else:
            # The only remaining variant is a resource link, which carries no inline content.
            label = block.title or block.name
            content.append(f"{label} ({block.uri})" if label else block.uri)
    return content


def skill_references(blocks: Sequence[PromptContentBlock]) -> tuple[str, ...]:
    """Skill ids a client attached to prompt blocks, in first-seen order."""
    ids: list[str] = []
    for block in blocks:
        meta = block.field_meta
        if not meta:
            continue
        payload = meta.get("hrns/skills")
        if not isinstance(payload, list):
            continue
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            skill_id = entry.get("id")
            if isinstance(skill_id, str) and skill_id and skill_id not in ids:
                ids.append(skill_id)
    return tuple(ids)
