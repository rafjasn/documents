from langchain_google_genai import ChatGoogleGenerativeAI

from docai.core.llm import chat_model
from docai.core.settings import Settings


def test_gemini_models_use_the_configured_key(settings: Settings) -> None:
    model = chat_model(settings.fast_model, settings)

    assert isinstance(model, ChatGoogleGenerativeAI)
    assert model.model == "gemini-test-fast"
    assert model.google_api_key is not None
    assert model.google_api_key.get_secret_value() == "test-gemini-key"


def test_explicit_arguments_are_passed_through(settings: Settings) -> None:
    model = chat_model(settings.chat_model, settings, temperature=0.0)

    assert isinstance(model, ChatGoogleGenerativeAI)
    assert model.temperature == 0.0
