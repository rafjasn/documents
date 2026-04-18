# Documents

A full-stack document management platform built on AWS.

## Features

- Drag-and-drop file upload (PDF, DOCX, PNG, JPG, WEBP, TXT, CSV, up to 50 MB) via S3 presigned URLs — no file data passes through the API server
- Automatic text extraction using pdf-parse, mammoth, and Tesseract OCR
- AI classification and summarisation powered by a pluggable LLM backend (Ollama, OpenAI, Anthropic, or AWS Bedrock)
- Thumbnail generation with sharp, in parallel with AI processing
- Real-time processing status pushed to the browser over WebSocket (Socket.IO) via SNS - SQS fan-out
- Authenticated document chat with streaming responses and persistent message history
- Pluggable auth provider: Keycloak locally, AWS Cognito in production

## Technologies

- **Frontend** — Next.js, Tailwind CSS, Socket.IO client
- **API** — NestJS, Socket.IO, Passport JWT, Swagger
- **Worker** — NestJS, SQS long-polling, pdf-parse, mammoth, Tesseract, sharp
- **Storage** — DynamoDB (single-table design), S3
- **Messaging** — SQS, SNS
- **AI** — Ollama, OpenAI, Anthropic, AWS Bedrock
- **Auth** — Keycloak, AWS Cognito
- **Infrastructure** — AWS CDK v2, ECS Fargate, ALB, ECR, CloudWatch
- **Local dev** — LocalStack, Docker Compose

## Local setup

### Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) ≥ 4 with Compose v2
- Node.js ≥ 20 and npm ≥ 10 (only needed if running outside Docker)

### 1. Clone and configure

```bash
git clone <repo-url>
cd documents
cp .env.example .env
```

The defaults work out of the box with LocalStack and Ollama. To use a cloud AI provider set `AI_PROVIDER` and the corresponding key:

| Variable | Default | Notes |
|---|---|---|
| `AI_PROVIDER` | `ollama` | `ollama` \| `openai` \| `anthropic` \| `bedrock` |
| `AI_MODEL` | `llama3.2:3b` | Model name for the chosen provider |
| `OPENAI_API_KEY` | — | Required when `AI_PROVIDER=openai` |
| `ANTHROPIC_API_KEY` | — | Required when `AI_PROVIDER=anthropic` |

### 2. Start all services

```bash
docker compose up --build
```

To run multiple replicas of the API and worker (traffic is distributed automatically):

```bash
docker compose up -d --scale api=3 --scale worker=2
```

| Service | URL |
|---|---|
| Frontend | http://localhost:3000 |
| API | http://localhost/api |
| Swagger UI | http://localhost/api/docs |
| Keycloak admin | http://localhost:8080 (admin / admin) |
| LocalStack | http://localhost:4566 |

LocalStack is initialised automatically — S3 bucket, DynamoDB table, SQS queues, and SNS topic are all created on first start.

### 3. Open the app

Go to http://localhost:3000, register an account, and upload a document. Processing status updates appear in real time.

## Testing

Unit and integration tests use Jest and run entirely offline — no Docker or AWS credentials required.

```bash
# All workspaces
npm test

# Single service
npm test -w @documents/api
npm test -w @documents/worker

# With coverage
npm run test:cov -w @documents/api
npm run test:cov -w @documents/worker
```

End-to-end tests for the API require LocalStack to be running (`docker compose up localstack -d`):

```bash
# API e2e (auth tests run without LocalStack; document tests require it)
npm run test:e2e -w @documents/api

# With coverage
npm run test:e2e:cov -w @documents/api
```

## Linting

```bash
# All workspaces
npm run lint

# Single service
npm run lint -w @documents/api
npm run lint -w @documents/worker
npm run lint -w @documents/frontend
```

## Deployment

Infrastructure is managed with AWS CDK v2 from the `infrastructure/cdk` directory.

### Required environment variables

| Variable | Required for | Description |
|---|---|---|
| `ALLOWED_ORIGINS` | synth, deploy | Comma-separated list of origins allowed by the S3 bucket CORS policy. Must include the frontend origin (ALB DNS, CloudFront domain, or custom domain). Synth fails loudly if missing so the bucket can't be deployed with `*`. |
| `CDK_DEFAULT_ACCOUNT` | deploy | AWS account ID (auto-populated when using `aws configure`). |
| `CDK_DEFAULT_REGION` | deploy | AWS region (defaults to `us-east-1`). |

### Validate (no AWS credentials required)

```bash
cd infrastructure/cdk
npm install
ALLOWED_ORIGINS=http://localhost:3000 \
CDK_DEFAULT_ACCOUNT=123456789012 \
CDK_DEFAULT_REGION=us-east-1 \
  npx cdk synth
```

### Bootstrap (once per account/region)

```bash
npx cdk bootstrap aws://<account-id>/<region>
```

### Deploy

```bash
# Stateful resources — VPC, S3, DynamoDB, SQS, SNS, ECR, IAM
ALLOWED_ORIGINS=https://app.example.com npx cdk deploy DocumentsInfraStack

# Services — ECS tasks, ALB, CloudWatch (CI/CD normally handles this)
ALLOWED_ORIGINS=https://app.example.com \
  npx cdk deploy DocumentsServicesStack --context imageTag=<git-sha>
```

The ALB DNS is assigned at deploy time. For a first-time deploy without a custom domain, deploy the services stack first, capture the `AlbDns` output, then redeploy the infra stack with `ALLOWED_ORIGINS=http://<alb-dns>`.

Before deploying services, put required secrets in AWS Secrets Manager:

```bash
aws secretsmanager put-secret-value --secret-id documents/jwt-secret \
  --secret-string "$(openssl rand -base64 32)"
```

### Destroy

```bash
npx cdk destroy --all --context removalPolicy=destroy
```

The `removalPolicy=destroy` context flag must be set at synth time for S3 and DynamoDB to be deleted with the stack.

### CI/CD

Two GitHub Actions workflows are included:

- **`ci.yml`** — runs on every push and PR: CDK synth, TypeScript build, Jest tests, Docker build check
- **`deploy.yml`** - builds and pushes Docker images to ECR, then deploys both CDK stacks via OIDC (no long-lived AWS credentials stored in GitHub)
