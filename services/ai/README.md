# AI service guide

Configuration, evaluation and operating commands for the Python chat service and ingestion worker. See [Architecture](../../docs/ARCHITECTURE.md) for the design and [Results](../../docs/RESULTS.md) for recorded measurements. Commands below run from `services/ai` unless stated otherwise.

## Configuration

The repository's [.env.example](../../.env.example) lists all settings.

| Settings | Purpose |
|---|---|
| `GEMINI_API_KEY`; `CHAT_MODEL`, `FAST_MODEL`, `VISION_MODEL`, `EXTRACT_MODEL`, `CONTEXT_MODEL`, `JUDGE_MODEL` | Direct provider credentials and model roles, using LangChain's `provider:model` format. Judges call the provider directly; gateway-backed services use the gateway key. |
| `INGEST_CACHE_DIR`, `TESSDATA_DIR`, `DOCLING_ARTIFACTS_PATH` | Content-hash cache and OCR/Docling model paths; images configure these for container runs. |
| `AWS_ENDPOINT`, `S3_BUCKET`, `DYNAMODB_TABLE`, `SQS_QUEUE_URL`, `SNS_TOPIC_ARN` | Storage and queue connections, shared with the API. |
| `QDRANT_URL`, `QDRANT_API_KEY`, `QDRANT_ALIAS`, `TEI_EMBED_URL`, `TEI_RERANK_URL` | Search services; default alias `chunks`. The reranker is optional for experiments. |
| `AUTH_JWKS_URI`, `AUTH_ISSUER`, `AUTH_AUDIENCE` | JWT validation; use the same identity provider as the API. |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL` | Enable tracing and remote prompt management. |
| `MASKING_KEY`, `LANGFUSE_PROMPT_LABEL` | Stable hashes for identifiers in traces; served prompt label, default `production`. Without a masking key each process uses a random key. |
| `LLM_GATEWAY_URL`, `LLM_GATEWAY_KEY` | Route chat and ingestion through LiteLLM instead of directly to Gemini. |

## Development

[uv](https://docs.astral.sh/uv/) installs the required Python version if needed.

```sh
uv sync --all-extras
uv run ruff check && uv run ruff format --check
uv run mypy
uv run pytest                                      # offline tests
uv run --env-file ../../.env pytest -m integration  # live provider calls
uv run --env-file ../../.env docai smoke            # provider/tracing smoke check
uv run --env-file ../../.env docai smoke --model chat
```

From the repository root, `docker compose up -d` starts the core services with Python source mounted for reload. The `ai` profile adds the experimental reranker. `docker/Dockerfile.ai` provides `ingest`, `serve` and `development` targets.

## Evaluation corpus

The corpus contains 40 CUAD contracts (CC BY 4.0), 60 SROIE receipts (research use only) and 100 synthetic invoices, receipts and contracts from five fictional companies in Polish and English. Synthetic files include digital PDFs, scans, phone photos and DOCX files with ground-truth fields and cross-document anomalies. SROIE stays out of the public demo and CI bundle.

The committed `corpus/manifest.jsonl` records source, licence, split and hashes; document files are rebuilt:

```sh
uv run docai corpus build
uv run docai corpus check       # file hashes and split leakage
node evals/legacy-extract.mjs   # extract the single-document baseline's text
```

The legacy extractor needs the repository's `npm ci` dependencies. PDFs and DOCX files use fixed dates and IDs. Noise in synthetic scans/photos uses an unseeded C-library generator, so those files may have different hashes on Linux from the macOS manifest.

## Evaluation methodology

- **Splits:** documents are assigned to train, dev and test; synthetic documents are grouped by company so related questions stay in one split. Pipeline choices use dev; final answer results use the 72 held-out test questions. All 200 documents remain in the search index as distractors.
- **Golden set:** `evals/golden/v1.jsonl` holds 144 hand-reviewed questions derived from ground truth: facts, tables, images, cross-document comparisons, aggregates, cross-lingual questions and unanswerable questions. Relevance labels identify documents and pages, so they survive chunking changes. Aggregate reference answers are computed from ground truth.
- **Baselines:** V0 receives the first 8,000 characters of the correct document; A1 searches the whole library and can compute over extracted fields. The comparison measures the change in application capability, with different context access.
- **Scoring:** rules check asserted numbers, units, dates, yes/no, refusals and accepted text forms. Negated or conflicting values and ambiguous answers go to the configured judge. Each recorded run identifies its judge; cached answers can be rescored with an audit of changed verdicts.
- **Interpretation:** this is a small domain-specific benchmark. One test question changes accuracy by about 1.4 percentage points. Model judgments have been spot-checked by hand; they are not independent human labels for every answer.

| Metric | Definition |
|---|---|
| Answer correctness | Share of questions accepted by deterministic rules or the judge; also broken down by question type and language. |
| Retrieval recall@k | Share of relevant document/page items found in the first k chunks; document recall ignores pages. |
| MRR@10 / nDCG@10 | Reciprocal rank of the first relevant hit / discounted gains for newly found relevant items. Questions with no relevant document are excluded. |
| Citation precision | Share of an answer's citations matching a labelled document and page where provided. |
| Faithfulness | Share of judged, cited answers whose claims are supported by their cited sources; refusals without citations are excluded. |
| Extraction | Normalised field matches and micro-averaged precision, recall and F1; fields empty in both truth and prediction are excluded. |
| System | p50/p95 latency, usage-based model cost per query and ingestion/indexing costs. Latency comparisons use uncached calls. |

Synthetic extraction scores cover all labelled fields; SROIE scores merchant, date and total. Costs use the repository's price table and recorded usage, independently of Langfuse. Attack evaluation separately reports tuned fixtures and held-out paraphrases; see [security results](../../docs/RESULTS.md#prompt-injection-and-tenant-isolation).

## Golden set and evaluation runs

```sh
uv run docai evals draft          # writes evals/golden/review-v1.csv
# Review question/answer/value/accepted; set decision=drop to exclude an item.
uv run docai evals apply-review   # writes evals/golden/v1.jsonl
uv run --env-file ../../.env docai evals run --variant V0 --split dev --dry-run
uv run --env-file ../../.env docai evals run --variant V0 --split dev --limit 10
uv run --env-file ../../.env docai evals run --variant A1 --split dev --record
uv run --env-file ../../.env docai evals faithfulness <run-id>
uv run docai evals report         # regenerates docs/RESULTS.md
```

In the review sheet, `value` is a decimal, ISO date, `yes`/`no`, `refusal` or text; `accepted` separates alternative text answers with ` | `. A1 evaluates as tenant `corpus`, loaded by `docai seed`. Responses are cached in `evals/cache/`; use `--no-cache` for measured latency.

After changing scoring rules, judge prompts or golden answers, rescore the stored responses:

```sh
uv run --env-file ../../.env docai evals rescore --dry-run
uv run --env-file ../../.env docai evals rescore --write
uv run docai evals report
```

Rescoring preserves each run's judge, may make uncached judge calls, updates answer/security runs and writes changed-verdict audits under `evals/audits/`. Review their `hand_check` column and synchronize README claims with the regenerated results.

## Ingestion worker

```sh
uv run docai ingest setup                                  # download OCR models
uv run --env-file ../../.env docai ingest file a.pdf b.jpg  # process local files
uv run --env-file ../../.env docai seed --wait              # seed the evaluation tenant
uv run --env-file ../../.env docai seed --reprocess --wait  # repeat after pipeline changes
uv run --env-file ../../.env docai evals extraction --split dev --record
```

Seeding writes a `PENDING` record and uploads to S3 under a stable document ID. It retries missing or failed documents and restores files after LocalStack loses S3 data. Sharing the ingestion cache with the worker reuses unchanged parse/model results. For the demo account and redistribution-safe source selection, follow [Demo setup](../../docs/DEMO.md#setup).

From the repository root, `node services/ai/evals/upload-flow.mjs <file>...` exercises API uploads and prints status events.

## Search index

```sh
uv run --env-file ../../.env docai index list
uv run --env-file ../../.env docai index build --stem 6 --switch
uv run --env-file ../../.env docai index switch --version 1   # return to a previous version
uv run --env-file ../../.env docai index drop --version 2
uv run --env-file ../../.env docai evals retrieval --variant V5-nr --record
```

Builds reuse S3 parses and catch up with concurrent uploads/deletions before switching the alias. A different embedding model requires its own TEI endpoint: build with `--tei-url`, then update `TEI_EMBED_URL` before switching. The [architecture](../../docs/ARCHITECTURE.md#upload-and-ingestion) explains collection versioning.

## Security and the CI evaluation

```sh
uv run docai security build
uv run --env-file ../../.env docai security seed
uv run --env-file ../../.env docai security run --variant A1 --record
uv run --env-file ../../.env docai evals ci
```

Attack fixtures contain planted instructions and false claims plus a canary in another tenant. `--variant unguarded` runs the baseline defences.

The CI workflow restores `evals/ci` into Qdrant and asks 30 dev questions; `evals/ci/config.json` sets `min_correct`. `docai evals ci-bundle` rebuilds that bundle after an index or extraction change. The workflow needs a `GEMINI_API_KEY` repository secret and runs manually or on pull requests labelled `eval`.

## Prompts and masking

Chat prompts live in `docai/agent/prompts.py`; Langfuse holds versions and labels:

```sh
uv run --env-file ../../.env docai prompts status
uv run --env-file ../../.env docai prompts push
uv run --env-file ../../.env docai evals ci --prompts candidate --record
uv run --env-file ../../.env docai prompts promote --evaluation evals/runs/<stamp>-ci.json
uv run --env-file ../../.env docai prompts rollback
```

Remote-label evaluation pins versions and hashes and fails if any prompt cannot be resolved. Promotion requires a passing CI evaluation and promotes exactly its recorded versions, even if `candidate` has moved. Rollback restores `previous`. Runtime reads `LANGFUSE_PROMPT_LABEL` through the SDK's 60-second cache and uses code prompts when remote prompts are unavailable; the cache refreshes in the background. Ingestion prompts stay in code because the chat CI bundle does not evaluate ingestion.

A manual **Evaluation** workflow with `prompts=candidate` and promotion enabled needs the `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` secrets and `LANGFUSE_BASE_URL` variable.

Masking applies to Langfuse observations, other exported spans and evaluation dataset items. It replaces supported identifiers with keyed hashes and redacts API keys in URLs. Document content still reaches the model provider and local caches; names and addresses are not masked. Use the [runtime checks](#runtime-checks) to inspect exported traces.

## LLM gateway

LiteLLM and its PostgreSQL budget store run in the `ops` profile. In the root `.env`, set `LLM_GATEWAY_URL=http://localhost:4000`, and set `LITELLM_MASTER_KEY`, `LLM_GATEWAY_KEY` and `LLM_GATEWAY_DB_PASSWORD` to distinct random strings starting with `sk-`, for example `sk-$(openssl rand -hex 24)`. From the repository root:

```sh
docker compose --profile ops up -d
(cd services/ai && uv run --env-file ../../.env docai gateway setup)
```

- `LLM_GATEWAY_USER_DAILY_BUDGET` defaults to $0.50 across a user's chat and uploads; `LLM_GATEWAY_DAILY_BUDGET` defaults to $5 globally. Both reset at midnight UTC.
- Over-budget chat returns an error with the reset time. An upload becomes `FAILED` and its queue message is dropped; upload again after the reset.
- Provider failures route to the configured fallback model; usage and traces record the model that answered. The clients do not retry independently.
- Virtual keys require the budget database; unavailable storage refuses calls. The LiteLLM image is pinned by digest.

## Telemetry

Set the `LANGFUSE_*` variables for traces; enable the `ops` profile for metrics, the dashboard and alerts.

| Endpoint | Purpose |
|---|---|
| AI service `/metrics` | Requests, graph timings and model usage |
| Worker port `9101` | Upload status, model usage and queue metrics |
| API port `9464` | Request metrics |
| Grafana at <http://localhost:3030> | Provisioned dashboard and alerts |

For a host evaluation, `METRICS_PORT=9102 uv run --env-file ../../.env docai evals ci` exposes its metrics to the `evals` Prometheus job. Grafana alerts cover chat p95 latency, question/upload errors, hourly spend, queue backlog and message age. See the [dashboard screenshot](../../docs/media/dashboard.png).

The [CDK guide](../../infrastructure/cdk/README.md#topology) describes the CloudWatch collectors and alarms. Cached reprocessing omits upload traces; host evaluations export model spans only.

## Runtime checks

These scripts exercise a running, seeded stack. Trace checks additionally need Langfuse; gateway/alert checks need the `ops` profile. Recorded checks feed [Results](../../docs/RESULTS.md#operations).

```sh
uv run docai graph
uv run --env-file ../../.env python evals/index_gate.py deletion <file>
uv run --env-file ../../.env python evals/index_gate.py reindex --stem 6
uv run --env-file ../../.env python evals/chat_e2e.py
uv run --env-file ../../.env python evals/masking_gate.py --since <time>
uv run --env-file ../../.env python evals/telemetry_gate.py chats --since <time>
uv run python evals/collector_check.py
```

Gateway and alert drills read environment variables directly and temporarily change service availability or configuration. Run them on the local stack, then restore its normal configuration:

```sh
set -a; source ../../.env; set +a
uv run python evals/gateway_routes.py
uv run python evals/budget_gate.py users
LLM_GATEWAY_DAILY_BUDGET=0.000001 docker compose --profile ops up -d llm-gateway
uv run python evals/budget_gate.py global
docker stop documents-llm-gateway-db
uv run python evals/budget_gate.py db
docker start documents-llm-gateway-db
LLM_GATEWAY_CONFIG=drill-primary-down.yaml docker compose --profile ops up -d llm-gateway
uv run python evals/gateway_routes.py --drill
uv run python evals/fallback_drill.py
docker compose --profile ops up -d llm-gateway
uv run python evals/alert_drill.py
```

From the repository root, `sh infrastructure/scripts/secret-scan.sh` checks history and files eligible for commit with gitleaks.
