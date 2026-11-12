"""Config parsing, including the Python 3.10 path where tomllib does not exist."""
import pytest

from sigil import config as config_mod


def test_roundtrip_through_save_and_load(cfg, tmp_path):
    cfg.promote_threshold = 0.7
    cfg.predicates = ("causes", "custom_pred")
    cfg.llm_model = "test-model"
    cfg.save()
    again = config_mod.load(cfg.root)
    assert again.promote_threshold == pytest.approx(0.7)
    assert again.predicates == ("causes", "custom_pred")
    assert again.llm_model == "test-model"


def test_mini_toml_matches_tomllib(cfg):
    """The 3.10 fallback must parse exactly what save() writes."""
    if config_mod.tomllib is None:
        pytest.skip("tomllib is 3.11+; the 3.10 path is covered by test_load_without_tomllib")
    cfg.predicates = ("causes", "depends_on", "custom_pred")
    cfg.promote_threshold = 0.55
    cfg.llm_api_key = "sk-not-a-real-key"
    cfg.save()
    text = cfg.config_path.read_text()
    assert config_mod._mini_toml(text) == config_mod.tomllib.loads(text)


def test_load_without_tomllib(cfg, monkeypatch):
    """Simulates Python 3.10: tomllib absent, everything still works."""
    cfg.promote_threshold = 0.65
    cfg.save()
    monkeypatch.setattr(config_mod, "tomllib", None)
    again = config_mod.load(cfg.root)
    assert again.promote_threshold == pytest.approx(0.65)
    assert "causes" in again.predicates


def test_mini_toml_rejects_unterminated_array():
    with pytest.raises(ValueError):
        config_mod._mini_toml("[quipu]\npredicates = [\n  \"a\",\n")
