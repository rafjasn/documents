#!/bin/sh
# Secret scan before a push to the public repository (Stage 11): gitleaks, pinned by
# digest, over the whole git history and over the files a commit would include (tracked,
# plus untracked files that aren't ignored, so never .env or the caches). Findings are
# redacted. Exits non-zero on any finding. Run from the repository root.
set -eu
IMAGE=ghcr.io/gitleaks/gitleaks:v8.30.1@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f
tree=$(mktemp -d)
trap 'rm -rf "$tree"' EXIT

docker run --rm -v "$PWD:/repo:ro" --entrypoint sh "$IMAGE" -c \
    'git config --global --add safe.directory /repo && gitleaks git /repo --redact --no-banner'

git ls-files -co --exclude-standard | while IFS= read -r file; do
    if [ -f "$file" ]; then
        mkdir -p "$tree/$(dirname "$file")" && cp "$file" "$tree/$file"
    fi
done
docker run --rm -v "$tree:/tree:ro" "$IMAGE" dir /tree --redact --no-banner
