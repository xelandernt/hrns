from harness_client.renderers import RendererRegistry, readable_text


def payload(renderer_id="demo.diff", version=1, data=None, fallback="fallback text"):
    return {
        "rendererId": renderer_id,
        "schemaVersion": version,
        "data": data or {"path": "a.py", "added": 2},
        "fallback": fallback,
    }


def test_local_renderer_and_version_exact_lookup():
    registry = RendererRegistry()
    registry.register("demo.diff", 1, lambda data: f"{data['path']}: +{data['added']}")
    assert registry.render(payload(), fallback="x") == "a.py: +2"
    # A remote payload cannot opt into an unregistered version/id or import path.
    assert registry.render(payload(version=2, fallback="safe"), fallback="outer") == "safe"
    assert registry.render(payload(renderer_id="os.system", fallback="safe"), fallback="outer") == "safe"


def test_duplicate_registration_is_explicit_and_failures_fall_back():
    registry = RendererRegistry()
    registry.register("demo", 1, lambda data: "ok")
    try:
        registry.register("demo", 1, lambda data: "other")
    except ValueError as exc:
        assert "Duplicate" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("duplicate renderer was accepted")

    def broken(_data):
        raise RuntimeError("renderer bug")

    registry.register("bad", 1, broken)
    assert registry.render(payload("bad", fallback="readable fallback"), fallback="outer") == "readable fallback"
    assert registry.render({"rendererId": "", "schemaVersion": 1, "data": {}, "fallback": "f"},
                           fallback="outer") == "outer"


def test_readable_text_strips_terminal_controls_and_bounds_output():
    rendered = readable_text("before\x1b[31mred\x1b[0m\x07 \u202eRTL")
    assert rendered == "beforered RTL"
    bounded = readable_text("x" * 100, limit=10)
    assert bounded.startswith("x" * 10)
    assert "truncated" in bounded


def test_rendered_text_is_sanitized_even_when_adapter_returns_controls():
    registry = RendererRegistry()
    registry.register("unsafe", 1, lambda data: "ok\x1b[2J\x00")
    assert registry.render(payload("unsafe"), fallback="safe") == "ok"
