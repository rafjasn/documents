import pytest
from pydantic import ValidationError

from docai.core.settings import Settings


def test_models_are_required() -> None:
    with pytest.raises(ValidationError) as error:
        Settings()
    missing = {entry["loc"][0] for entry in error.value.errors()}
    assert missing == {"chat_model", "fast_model"}


def test_blank_gemini_key_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "  ")
    monkeypatch.setenv("CHAT_MODEL", "google_genai:chat")
    monkeypatch.setenv("FAST_MODEL", "google_genai:fast")
    with pytest.raises(ValidationError, match="GEMINI_API_KEY is required"):
        Settings()


def test_behind_the_gateway_the_gemini_key_is_optional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # As in AWS: only the gateway holds the Gemini key.
    monkeypatch.setenv("LLM_GATEWAY_URL", "http://llm-gateway:4000")
    settings = Settings(chat_model="a:b", fast_model="a:c")
    assert settings.gemini_api_key is None
    with pytest.raises(ValueError, match="through the gateway"):
        settings.gemini_key


def test_settings_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "key")
    monkeypatch.setenv("CHAT_MODEL", "google_genai:chat")
    monkeypatch.setenv("FAST_MODEL", "google_genai:fast")
    settings = Settings()
    assert settings.gemini_key == "key"
    assert (settings.chat_model, settings.fast_model) == (
        "google_genai:chat",
        "google_genai:fast",
    )


def test_blank_langfuse_variables_count_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # docker compose passes `${LANGFUSE_PUBLIC_KEY:-}` through as an empty string.
    for name in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL"):
        monkeypatch.setenv(name, "")
    settings = Settings(gemini_api_key="key", chat_model="a:b", fast_model="a:c")
    assert settings.missing_langfuse_variables == [
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_BASE_URL",
    ]


def test_partial_langfuse_configuration_lists_what_is_missing(
    settings: Settings,
) -> None:
    partial = settings.model_copy(update={"langfuse_public_key": "pk-lf-test"})
    assert partial.missing_langfuse_variables == [
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_BASE_URL",
    ]
