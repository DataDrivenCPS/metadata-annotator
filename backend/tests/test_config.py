import pytest

from workbench.config import load_settings


def test_agent_limits_default_and_override(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text("")
    defaults = load_settings(path).agent
    assert (defaults.max_steps, defaults.max_repairs) == (8, 2)
    path.write_text("[agent]\nmax_steps = 20\nmax_repairs = 0\n")
    configured = load_settings(path).agent
    assert (configured.max_steps, configured.max_repairs) == (20, 0)


def test_provider_schema_strictness_can_be_disabled(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text('[llm.providers.luna]\nkind = "openai"\nmodel = "gpt-6-luna"\n'
                    'base_url = "https://api.openai.com/v1"\nstrict_schema = false\n')
    settings = load_settings(path)
    assert settings.provider("luna").strict_schema is False
    assert settings.provider("local").strict_schema is True


@pytest.mark.parametrize("name,value", [
    ("max_steps", "0"), ("max_steps", "-1"), ("max_steps", "1.5"),
    ("max_steps", "true"), ("max_steps", '"20"'),
    ("max_repairs", "-1"), ("max_repairs", "false"),
])
def test_invalid_agent_limits_fail_at_load(tmp_path, name, value):
    path = tmp_path / "settings.toml"
    path.write_text(f"[agent]\n{name} = {value}\n")
    with pytest.raises(ValueError, match=f"agent.{name} must be an integer"):
        load_settings(path)
