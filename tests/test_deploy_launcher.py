"""部署包启动器向导的纯函数测试（不触发重型依赖 import）。"""
from __future__ import annotations

from deploy.launcher import PROVIDERS, needs_config, render_env


def test_render_env_contains_all_fields():
    text = render_env("openai_compatible", "https://api.example.com/v1", "sk-x", "model-vl", "secret-123")
    assert "AI_PROVIDER=openai_compatible" in text
    assert "AI_BASE_URL=https://api.example.com/v1" in text
    assert "AI_API_KEY=sk-x" in text
    assert "AI_MODEL=model-vl" in text
    assert "AUTH_SECRET=secret-123" in text


def test_needs_config_when_env_missing(tmp_path):
    assert needs_config(tmp_path / ".env") is True


def test_needs_config_when_key_empty(tmp_path):
    env = tmp_path / ".env"
    env.write_text(render_env("mock", "", "", "", "secret"), encoding="utf-8")
    assert needs_config(env) is True


def test_needs_config_false_when_key_present(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        render_env("openai_compatible", "https://api.example.com/v1", "sk-x", "m", "s"),
        encoding="utf-8",
    )
    assert needs_config(env) is False


def test_provider_presets_have_required_fields():
    assert len(PROVIDERS) >= 4
    for preset in PROVIDERS:
        assert preset["provider"] in ("openai_compatible", "gemini")
        assert preset["model"]
        assert preset["signup"]
