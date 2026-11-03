"""Chat models built from configured "<provider>:<model>" specs."""

from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage

from docai.core.gateway import gateway_chat
from docai.core.settings import CoreSettings


def chat_model(
    spec: str, settings: CoreSettings, *, direct: bool = False, **kwargs: Any
) -> BaseChatModel:
    """A chat model for a spec such as settings.fast_model.

    With LLM_GATEWAY_URL set, the model is called through the LLM gateway
    (docai.core.gateway) unless `direct`. Otherwise Gemini models get the key from
    settings, and other providers read their own environment variables, so switching
    provider is a configuration change.
    """
    if settings.llm_gateway_url and not direct:
        if settings.llm_gateway_key is None:
            raise ValueError("LLM_GATEWAY_URL is set but LLM_GATEWAY_KEY isn't")
        return gateway_chat(
            spec,
            settings.llm_gateway_url,
            settings.llm_gateway_key.get_secret_value(),
            **kwargs,
        )
    if spec.partition(":")[0] == "google_genai":
        kwargs.setdefault("api_key", settings.gemini_key)
    model: BaseChatModel = init_chat_model(spec, **kwargs)
    return model


def answered_by(spec: str, message: BaseMessage) -> str:
    """The spec of the model that wrote `message`: through the gateway, the fallback
    answers when `spec`'s model fails, and is priced as itself."""
    provider, _, name = spec.rpartition(":")
    model = str(message.response_metadata.get("model_name") or "")
    if not model or model == name:
        return spec
    return f"{provider}:{model}" if provider else model
