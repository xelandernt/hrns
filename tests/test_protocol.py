import pytest
from harness_protocol import (
    ConfigurationSnapshot,
    ConfigurationUpdate,
    HarnessError,
    canonical_hash,
    changes_adapter,
    decode_frame,
    encode_frame,
    paginate,
)
from pydantic import ValidationError


def test_changes_have_explicit_discriminators_and_camel_case():
    update = ConfigurationUpdate.model_validate({
        "sessionId": "s", "requestId": "r", "expectedRevision": 0,
        "changes": [{"type": "enable", "bindingId": "web", "enabled": False}],
    })
    assert update.to_wire()["changes"] == [{"type": "enable", "bindingId": "web", "enabled": False}]
    with pytest.raises(ValidationError):
        changes_adapter.validate_python([{"bindingId": "web", "enabled": True}])
    with pytest.raises(ValidationError):
        changes_adapter.validate_python([{"type": "enable", "bindingId": "web", "enabled": True, "import": "os"}])


def test_nullable_replacements_preserve_null():
    update = ConfigurationUpdate(session_id="s", request_id="r", expected_revision=2,
                                 changes=[{"type": "configure", "bindingId": "x", "fields": {"nullable": None}}])
    assert update.to_wire()["changes"][0]["fields"] == {"nullable": None}


def test_snapshots_roundtrip():
    snapshot = ConfigurationSnapshot(session_id="s", selected_revision=4, active_revision=3,
                                     bindings={"web": {"enabled": False}}, application_status="pending")
    assert ConfigurationSnapshot.model_validate(snapshot.to_wire()) == snapshot
    assert snapshot.to_wire()["selectedRevision"] == 4


@pytest.mark.parametrize("raw", ['[]', '{}', '{"jsonrpc":"1.0"}', 'invalid', b'\xff'])
def test_invalid_frames(raw):
    with pytest.raises(HarnessError, match="Expected"):
        decode_frame(raw)


def test_framing_and_canonical_hash():
    message = {"jsonrpc": "2.0", "method": "session/update", "params": {"text": "héllo"}}
    assert decode_frame(encode_frame(message)) == message
    assert canonical_hash({"b": 2, "a": 1}) == canonical_hash({"a": 1, "b": 2})


@pytest.mark.parametrize("raw", [
    '{"jsonrpc":"2.0"}',
    '{"jsonrpc":"2.0","method":"x","id":true}',
    '{"jsonrpc":"2.0","method":"x","params":null}',
    '{"jsonrpc":"2.0","method":"x","method":"y"}',
    '{"jsonrpc":"2.0","id":1,"result":NaN}',
    '{"jsonrpc":"2.0","id":1,"result":{},"error":{}}',
    '{"jsonrpc":"2.0","id":1,"error":{"code":true,"message":"bad"}}',
])
def test_json_rpc_envelope_rejects_ambiguous_data(raw):
    with pytest.raises(HarnessError):
        decode_frame(raw)


def test_response_framing():
    for message in [
        {"jsonrpc": "2.0", "id": 1, "result": None},
        {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
    ]:
        assert decode_frame(encode_frame(message)) == message


def test_invalid_unicode_and_depth_are_application_errors():
    for raw in ['{"jsonrpc":"2.0","method":"\ud800"}', '[' * 100_000 + ']' * 100_000]:
        with pytest.raises(HarnessError):
            decode_frame(raw)
    assert canonical_hash({"text": "\ud800"}) == canonical_hash({"text": "\ud800"})


def test_binding_descriptor_lifecycle_has_one_authority():
    from harness_protocol import BindingDescriptor, BindingListResponse

    descriptor = BindingDescriptor(binding_id="x", change_mode="next_turn",
                                   policy={"changeMode": "new_session"})
    assert descriptor.change_mode == "new_session"
    assert BindingListResponse(items=[descriptor]).to_wire()["items"][0]["policy"]["changeMode"] == "new_session"


def test_pagination_and_invalid_cursors():
    page, cursor = paginate(list(range(7)), limit=3)
    assert page == [0, 1, 2]
    page, cursor = paginate(list(range(7)), cursor, 3)
    assert page == [3, 4, 5]
    assert paginate(list(range(7)), cursor, 3) == ([6], None)
    for cursor in ["nonsense", "W10=", "eyJ2IjoxLCJvZmZzZXQiOi0xfQ=="]:
        with pytest.raises(HarnessError):
            paginate([], cursor)
    with pytest.raises(HarnessError):
        paginate([], limit=101)


def test_application_error_shape():
    error = HarnessError("POLICY_DENIED", "Locked", {"bindingId": "shell"}).to_rpc()
    assert error["code"] == -32000
    assert error["data"]["code"] == "POLICY_DENIED"
    assert "_meta" not in error
