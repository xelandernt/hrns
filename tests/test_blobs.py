import pytest
from harness_protocol import HarnessError
from harness_runtime.blobs import FileBlobStore


def test_content_addressing_and_owner_isolation(tmp_path):
    store = FileBlobStore(tmp_path)
    ref = store.put("alice", "session", b"hello", name="../../hello.txt", media_type="text/plain")
    assert ref.name == "hello.txt"
    assert store.get("alice", "session", ref) == b"hello"
    assert store.put("alice", "session", b"hello").sha256 == ref.sha256
    with pytest.raises(HarnessError, match="unavailable"):
        store.get("bob", "session", ref)
    with pytest.raises(HarnessError, match="unavailable"):
        store.get("alice", "other", ref)


def test_bounded_blobs_and_separate_deletion(tmp_path):
    store = FileBlobStore(tmp_path, max_bytes=4)
    with pytest.raises(HarnessError, match="limit"):
        store.put("alice", "one", b"12345")
    one = store.put("alice", "one", b"1234")
    two = store.put("alice", "two", b"1234")
    store.delete_session("alice", "one")
    with pytest.raises(HarnessError):
        store.get("alice", "one", one)
    assert store.get("alice", "two", two) == b"1234"


def test_integrity_and_opaque_namespace(tmp_path):
    store = FileBlobStore(tmp_path)
    ref = store.put("../../escape", "/tmp/session", b"secret")
    files = list(tmp_path.rglob(ref.sha256))
    assert len(files) == 1
    files[0].write_bytes(b"broken")
    with pytest.raises(HarnessError, match="integrity"):
        store.get("../../escape", "/tmp/session", ref)
