from collections.abc import Iterator

import pytest

from docai.core.settings import LANGFUSE_VARIABLES, Settings
from tests.aws_support import Aws, mock_stack

CONFIG_VARIABLES = (
    "GEMINI_API_KEY",
    "CHAT_MODEL",
    "FAST_MODEL",
    "LLM_GATEWAY_URL",
    "LLM_GATEWAY_KEY",
    *LANGFUSE_VARIABLES,
)


@pytest.fixture(autouse=True)
def isolated_environment(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unit tests never see a developer's real keys; integration tests need them."""
    if request.node.get_closest_marker("integration"):
        return
    for name in CONFIG_VARIABLES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        gemini_api_key="test-gemini-key",
        chat_model="google_genai:gemini-test-chat",
        fast_model="google_genai:gemini-test-fast",
    )


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[Aws]:
    yield from mock_stack(monkeypatch)
