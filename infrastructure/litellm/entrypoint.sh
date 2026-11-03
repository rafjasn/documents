#!/bin/sh
# The gateway's spans go to Langfuse beside the services' (Stage 10c) when its keys are
# set; LiteLLM's OpenTelemetry callback needs the OTLP endpoint and a basic-auth header.
# Without the keys the OpenTelemetry SDK is off, so the callback exports nothing.
set -eu
if [ -n "${LANGFUSE_PUBLIC_KEY:-}" ] && [ -n "${LANGFUSE_SECRET_KEY:-}" ] && [ -n "${LANGFUSE_BASE_URL:-}" ]; then
    credentials=$(printf %s "$LANGFUSE_PUBLIC_KEY:$LANGFUSE_SECRET_KEY" | base64 -w0)
    export OTEL_EXPORTER=otlp_http
    export OTEL_ENDPOINT="${LANGFUSE_BASE_URL%/}/api/public/otel/v1/traces"
    export OTEL_HEADERS="Authorization=Basic $credentials"
else
    export OTEL_SDK_DISABLED=true
fi
exec docker/prod_entrypoint.sh "$@"
