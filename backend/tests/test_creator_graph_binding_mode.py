from backend.config import Settings, settings


def test_compiled_v2_is_default(monkeypatch):
    monkeypatch.delenv("CREATOR_GRAPH_BINDING_MODE", raising=False)
    assert Settings().creator_graph_binding_mode == "compiled_v2"
    assert settings.creator_graph_binding_mode == "compiled_v2"


def test_modes_are_explicit_environment_choices(monkeypatch):
    for mode in ("legacy", "shadow", "compiled_v2"):
        monkeypatch.setenv("CREATOR_GRAPH_BINDING_MODE", mode)
        assert Settings().creator_graph_binding_mode == mode
