"""Stage 09: the collectors of the AWS stacks, run against the local stack.

Renders each collector's configuration from the CDK code (infrastructure/cdk/lib/
collector.ts), points its exporter at a local stand-in for CloudWatch's OTLP endpoint
instead of AWS, and runs the pinned collector beside the API, the ai-service and the worker,
as ECS would. It checks what CloudWatch would receive: SigV4-signed requests for the
`monitoring` service, gzip, histograms with their buckets, and how many bytes a scrape is,
which the cost estimate uses. Nothing is sent to AWS; the credentials are fake.

From services/ai, with the stack up:
    uv run python evals/collector_check.py
"""

import gzip
import json
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)

REPO = Path(__file__).resolve().parents[3]
CDK = REPO / "infrastructure" / "cdk"
RUNS = Path(__file__).parent / "runs"
# The compose service, its metrics port (as in services-stack.ts).
TARGETS = {"api": 9464, "ai-service": 8000, "ingestion-worker": 9101}
SINK_PORT = 4319
SCRAPES = 2
LIMIT_SECONDS = 240
CUMULATIVE = 2  # AggregationTemporality

received: list[dict[str, Any]] = []


class Sink(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        gzipped = self.headers.get("Content-Encoding") == "gzip"
        raw = gzip.decompress(body) if gzipped else body
        request = ExportMetricsServiceRequest.FromString(raw)
        auth = self.headers.get("Authorization", "")
        services, histograms, points = set(), [], 0
        for resource in request.resource_metrics:
            services |= {
                a.value.string_value
                for a in resource.resource.attributes
                if a.key == "service.name"
            }
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    data = getattr(metric, metric.WhichOneof("data"))
                    points += len(data.data_points)
                    if metric.WhichOneof("data") == "histogram" and data.data_points:
                        point = data.data_points[0]
                        histograms.append(
                            {
                                "name": metric.name,
                                "cumulative": data.aggregation_temporality
                                == CUMULATIVE,
                                "bounds": len(point.explicit_bounds),
                                "buckets": len(point.bucket_counts),
                            }
                        )
        received.append(
            {
                "service": ",".join(sorted(services)),
                "path": self.path,
                "sigv4_monitoring": auth.startswith("AWS4-HMAC-SHA256")
                and "/monitoring/aws4_request" in auth,
                "gzip": gzipped,
                "bytes": len(raw),
                "datapoints": points,
                "histograms": histograms,
            }
        )
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


def config(job: str, port: int) -> str:
    script = (
        "process.stdout.write(require('./lib/collector')"
        f".collectorConfig('{job}', {port}, 'us-east-1'))"
    )
    rendered = subprocess.run(
        ["npx", "ts-node", "--transpile-only", "-e", script],
        cwd=CDK,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    endpoint = "metrics_endpoint: https://monitoring.us-east-1.amazonaws.com/v1/metrics"
    assert rendered.count(endpoint) == 1, rendered
    return rendered.replace(
        endpoint,
        f"metrics_endpoint: http://host.docker.internal:{SINK_PORT}/v1/metrics",
    )


def image() -> str:
    source = (CDK / "lib" / "collector.ts").read_text()
    return source.split("COLLECTOR_IMAGE =\n    '", 1)[1].split("'", 1)[0]


def container_of(service: str) -> str:
    found = subprocess.run(
        ["docker", "compose", "ps", "-q", service],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not found:
        raise SystemExit(f"{service} isn't running: start the stack first")
    return found


def memory(name: str) -> str:
    return subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", name],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def main() -> int:
    server = HTTPServer(("0.0.0.0", SINK_PORT), Sink)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    collector = image()
    names = {job: f"collector-check-{job}" for job in TARGETS}
    try:
        for job, port in TARGETS.items():
            subprocess.run(
                ["docker", "run", "-d", "--name", names[job]]
                + ["--network", f"container:{container_of(job)}", "--memory", "128m"]
                + ["-e", "AWS_ACCESS_KEY_ID=fake", "-e", "AWS_SECRET_ACCESS_KEY=fake"]
                + ["-e", f"AOT_CONFIG_CONTENT={config(job, port)}", collector],
                check=True,
                capture_output=True,
            )
            print(f"collector beside {job}, scraping localhost:{port}", flush=True)
        started = time.monotonic()
        while time.monotonic() - started < LIMIT_SECONDS:
            counts = {j: sum(r["service"] == j for r in received) for j in TARGETS}
            if min(counts.values()) >= SCRAPES:
                break
            time.sleep(2)
        memories = {job: memory(name) for job, name in names.items()}
    finally:
        for name in names.values():
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        server.shutdown()

    per_service = {}
    for job in TARGETS:
        mine = [r for r in received if r["service"] == job]
        per_service[job] = {
            "requests": len(mine),
            "bytes_per_scrape": max((r["bytes"] for r in mine), default=0),
            "datapoints_per_scrape": max((r["datapoints"] for r in mine), default=0),
            "histograms": len(mine[-1]["histograms"]) if mine else 0,
            "memory": memories[job],
        }
        print(f"{job}: {per_service[job]}")
    checks = {
        "every service sent at least two scrapes": all(
            s["requests"] >= SCRAPES for s in per_service.values()
        ),
        "every request is SigV4-signed for monitoring": all(
            r["sigv4_monitoring"] for r in received
        ),
        "every request is gzipped OTLP to /v1/metrics": all(
            r["gzip"] and r["path"] == "/v1/metrics" for r in received
        ),
        "histograms arrive cumulative, with their buckets": all(
            h["cumulative"] and h["buckets"] == h["bounds"] + 1 and h["bounds"] > 0
            for r in received
            for h in r["histograms"]
        )
        and any(r["histograms"] for r in received),
    }
    for name, ok in checks.items():
        print(f"{'PASS' if ok else 'FAIL'}: {name}")
    passed = all(checks.values())
    RUNS.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    path = RUNS / f"{stamp}-collector-check.json"
    record = {
        "kind": "collector-check",
        "collector": collector,
        "services": per_service,
        "checks": checks,
        "passed": passed,
    }
    path.write_text(json.dumps(record, indent=2) + "\n")
    print(f"{'PASS' if passed else 'FAIL'}; saved {path}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
