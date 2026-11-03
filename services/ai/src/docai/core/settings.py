"""Runtime configuration, read from environment variables.

Each entry point reads only the settings it needs: `ServiceSettings` for the AI
service, `Settings` for evaluation runs, `PipelineSettings` for running the ingestion
pipeline on files, `AwsSettings` for the seed command, `IndexSettings` for search
infrastructure and `WorkerSettings` for the ingestion worker.
"""

from pathlib import Path
from typing import Self

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LANGFUSE_VARIABLES = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL")
# services/ai in a source checkout; deployed images set the paths below explicitly.
SERVICE_ROOT = Path(__file__).resolve().parents[3]


class Environment(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    @field_validator("*", mode="before")
    @classmethod
    def blank_is_unset(cls, value: object) -> object:
        """Compose passes unset variables through as empty strings; treat them as unset."""
        return None if isinstance(value, str) and not value.strip() else value


class CoreSettings(Environment):
    """What every model-calling entry point needs.

    Langfuse is optional: tracing is on only when all three LANGFUSE_* variables are set.
    GEMINI_API_KEY is required unless LLM_GATEWAY_URL is set: behind the gateway, as in
    AWS, only the gateway holds the key.
    """

    gemini_api_key: SecretStr | None = None
    langfuse_public_key: str | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_base_url: str | None = None
    # Keys the hashes that replace identifiers in traces (docai.core.masking). Unset, each
    # process uses a random key, so the same value hashes differently across runs.
    masking_key: SecretStr | None = None
    # The LLM gateway (docai.core.gateway): set, model calls go through it with this key;
    # unset, straight to Gemini.
    llm_gateway_url: str | None = None
    llm_gateway_key: SecretStr | None = None

    @model_validator(mode="after")
    def gemini_key_or_gateway(self) -> Self:
        if self.gemini_api_key is None and self.llm_gateway_url is None:
            raise ValueError("GEMINI_API_KEY is required unless LLM_GATEWAY_URL is set")
        return self

    @property
    def gemini_key(self) -> str:
        """The Gemini key, for calls made straight to Gemini."""
        if self.gemini_api_key is None:
            raise ValueError(
                "GEMINI_API_KEY isn't set: model calls go through the gateway"
            )
        return self.gemini_api_key.get_secret_value()

    @property
    def missing_langfuse_variables(self) -> list[str]:
        values = (
            self.langfuse_public_key,
            self.langfuse_secret_key,
            self.langfuse_base_url,
        )
        return [
            name for name, value in zip(LANGFUSE_VARIABLES, values) if value is None
        ]


class Settings(CoreSettings):
    """The AI service and evaluation runs.

    Models are always configured, never hard-coded. CHAT_MODEL, FAST_MODEL and
    JUDGE_MODEL use LangChain's "<provider>:<model>" form, e.g.
    "google_genai:gemini-3.1-flash-lite". JUDGE_MODEL is needed only by evaluation runs.
    """

    chat_model: str
    fast_model: str
    judge_model: str | None = None


class AwsSettings(Environment):
    """AWS resources shared with the NestJS API. AWS_ENDPOINT points at LocalStack.

    Credentials come from boto3's usual chain (AWS_ACCESS_KEY_ID and friends, or the
    task role in AWS). The defaults match the NestJS services and LocalStack.
    """

    aws_region: str = "us-east-1"
    aws_endpoint: str | None = None
    s3_bucket: str = "documents-uploads"
    dynamodb_table: str = "documents-documents"
    sqs_queue_url: str = (
        "http://sqs.us-east-1.localhost.localstack.cloud:4566"
        "/000000000000/documents-processing"
    )
    sns_topic_arn: str = "arn:aws:sns:us-east-1:000000000000:documents-notifications"


class PipelineSettings(CoreSettings):
    """The ingestion pipeline: parsing, OCR, figure captions and extraction.

    VISION_MODEL captions figures and page images; EXTRACT_MODEL classifies each
    document and extracts its fields and summary. INGEST_CACHE_DIR keeps parse results
    and model replies on disk by content hash, so reprocessing a file makes no calls;
    leave it unset to cache nothing.
    """

    vision_model: str
    extract_model: str
    tessdata_dir: Path = SERVICE_ROOT / ".cache" / "tessdata"
    ingest_cache_dir: Path | None = None
    docling_artifacts_path: Path | None = None
    ingest_threads: int = 2


class IndexSettings(Environment):
    """Search infrastructure: Qdrant and the TEI embedding and reranking servers.

    QDRANT_ALIAS names the alias that points at the live index version. CONTEXT_MODEL
    writes chunk contexts for contextual retrieval; unset, EXTRACT_MODEL does.
    """

    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: SecretStr | None = None
    qdrant_alias: str = "chunks"
    tei_embed_url: str = "http://localhost:8081"
    tei_rerank_url: str = "http://localhost:8082"
    context_model: str | None = None


class GatewaySettings(Environment):
    """`docai gateway setup`: the gateway's admin key, the services' key it creates, and
    the daily budget each user gets (USD)."""

    litellm_master_key: SecretStr
    llm_gateway_key: SecretStr
    llm_gateway_user_daily_budget: float = 0.5


class AuthSettings(Environment):
    """JWT validation: the same key set, issuer and audience as the API (Keycloak
    locally, Cognito in AWS)."""

    auth_jwks_uri: str
    auth_issuer: str
    auth_audience: str


class ServiceSettings(Settings, IndexSettings, AwsSettings, AuthSettings):
    """The AI service: chat models, search, the documents table, and token checks.

    LANGFUSE_PROMPT_LABEL picks the prompt versions it serves from Langfuse when tracing
    is on (docai.agent.prompt_store); without Langfuse it serves the code's prompts.
    """

    langfuse_prompt_label: str = "production"


class WorkerSettings(PipelineSettings, AwsSettings, IndexSettings):
    """The ingestion worker: the pipeline plus the processing queue.

    The visibility timeout matches the queue's (120 s, as for the NestJS worker); the
    worker extends it while a long document is still being processed.
    """

    sqs_visibility_timeout: int = 120
    sqs_wait_seconds: int = 10
    metrics_port: int | None = (
        9101  # Prometheus metrics (docai.core.telemetry); None: off
    )
    health_file: Path = Path("/tmp/ingestion-worker-alive")  # see docai.ingest.health
