import * as cdk from 'aws-cdk-lib';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';

/**
 * The OpenTelemetry collector (ADOT) beside each service with metrics: it scrapes the
 * service's Prometheus endpoint, as Prometheus does locally (Stage 10c), and sends the
 * metrics to CloudWatch's OTLP endpoint, where PromQL queries and alarms read them.
 * Histograms arrive with their buckets, so p95 latency can be computed; the EMF exporter
 * would have sent them as running totals without buckets. Traces don't pass through it:
 * each service's Langfuse exporter sends its own, as locally, after the in-process masking.
 */
export const COLLECTOR_IMAGE =
    'public.ecr.aws/aws-observability/aws-otel-collector:v0.50.0@sha256:7968fb60db6a2390a47ba6a2df029745638486e285c9b2487da1b722d0855a3e';

/** Once a minute: the alarms look at 5 to 60 minute windows. */
export const SCRAPE_INTERVAL_SECONDS = 60;

export function collectorConfig(job: string, port: number, region: string): string {
    return `extensions:
  sigv4auth:
    region: ${region}
    service: monitoring
receivers:
  prometheus:
    config:
      scrape_configs:
        - job_name: ${job}
          scrape_interval: ${SCRAPE_INTERVAL_SECONDS}s
          static_configs:
            - targets: ['localhost:${port}']
processors:
  batch: {}
exporters:
  otlphttp:
    metrics_endpoint: https://monitoring.${region}.amazonaws.com/v1/metrics
    compression: gzip
    auth:
      authenticator: sigv4auth
service:
  extensions: [sigv4auth]
  pipelines:
    metrics:
      receivers: [prometheus]
      processors: [batch]
      exporters: [otlphttp]
`;
}

/** Adds the collector to a task and lets the task's role send metrics. */
export function addCollector(
    task: ecs.FargateTaskDefinition,
    job: string,
    port: number,
    logGroup: logs.ILogGroup
): ecs.ContainerDefinition {
    task.addToTaskRolePolicy(
        new iam.PolicyStatement({
            // PutMetricData has no resource-level permissions.
            actions: ['cloudwatch:PutMetricData'],
            resources: ['*']
        })
    );
    return task.addContainer('collector', {
        image: ecs.ContainerImage.fromRegistry(COLLECTOR_IMAGE),
        // Losing metrics for a while is better than stopping the service.
        essential: false,
        memoryLimitMiB: 128,
        environment: {
            AOT_CONFIG_CONTENT: collectorConfig(job, port, cdk.Stack.of(task).region)
        },
        logging: ecs.LogDrivers.awsLogs({ logGroup, streamPrefix: 'collector' })
    });
}
