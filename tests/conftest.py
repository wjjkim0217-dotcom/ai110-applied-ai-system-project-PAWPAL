import pytest

PROVIDER_VARS = [
    "PAWPAL_PROVIDER", "PAWPAL_MODEL", "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
]


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    """Tests never see the developer's .env or real keys, so they behave the same on
    every machine and can never make a real API call by accident."""
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)
    for var in PROVIDER_VARS:
        monkeypatch.delenv(var, raising=False)
