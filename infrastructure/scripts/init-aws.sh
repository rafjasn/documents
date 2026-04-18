#!/bin/bash
set -euo pipefail

echo "Waiting for LocalStack..."
until curl -sf http://localhost:4566/_localstack/health | grep -q '"s3": "available"'; do
  sleep 2
done
echo "LocalStack ready"

export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test
export AWS_DEFAULT_REGION=us-east-1
export AWS_ENDPOINT_URL=http://localhost:4566

bash "$(dirname "$0")/../localstack/init-aws.sh"
