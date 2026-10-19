from pathlib import Path

import pytest
from harness_client.attachments import MAX_ATTACHMENT_BYTES, build_prompt_blocks
from harness_client.errors import HarnessClientError


def test_text_and_binary_attachments_are_embedded_content(tmp_path):
    text = tmp_path / "notes.md"
    text.write_text("# Context\n\nHello.", encoding="utf-8")
    image = tmp_path / "pixel.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    blocks = build_prompt_blocks("Use these files.", [str(text), str(image)])
    assert blocks[0].type == "text"
    assert blocks[0].text == "Use these files."
    assert blocks[1].type == "resource"
    assert blocks[1].resource.text == "# Context\n\nHello."
    assert blocks[1].resource.mime_type == "text/markdown"
    assert blocks[2].resource.uri == image.as_uri()
    assert blocks[2].resource.mime_type == "image/png"
    assert blocks[2].resource.blob


def test_directory_missing_and_oversized_attachments_rejected(tmp_path):
    with pytest.raises(HarnessClientError, match="regular files"):
        build_prompt_blocks("x", [str(tmp_path)])
    with pytest.raises(HarnessClientError, match="Cannot attach"):
        build_prompt_blocks("x", [str(tmp_path / "missing.txt")])

    big = tmp_path / "big.bin"
    with big.open("wb") as handle:
        handle.truncate(MAX_ATTACHMENT_BYTES + 1)
    with pytest.raises(HarnessClientError, match="per-file limit"):
        build_prompt_blocks("x", [str(big)])


def test_empty_prompt_is_not_replaced_by_attachment(tmp_path):
    path = tmp_path / "file.txt"
    path.write_text("content")
    with pytest.raises(HarnessClientError, match="non-empty text"):
        build_prompt_blocks("   ", [str(path)])


def test_non_utf8_text_named_file_is_sent_as_binary(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_bytes(b"\xff\xfe\x00")
    blocks = build_prompt_blocks("Read", [str(path)])
    assert not hasattr(blocks[1].resource, "text")
    assert blocks[1].resource.blob


def test_attachment_summary_does_not_read_content(tmp_path):
    from harness_client.attachments import attachment_summary

    path = tmp_path / "a.json"
    path.write_text("{}")
    summary = attachment_summary([str(path)])
    assert summary == [{
        "name": "a.json",
        "path": str(path.resolve()),
        "size": 2,
        "mimeType": "application/json",
    }]
    assert Path(summary[0]["path"]).exists()
