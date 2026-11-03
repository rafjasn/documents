/**
 * The monthly cost estimate for the stacks, which aren't deployed (README.md, Cost estimate).
 * Quantities come from the synthesized templates, so the estimate follows the stacks; prices
 * are US East (N. Virginia) list prices; usage that no template states is an assumption, named.
 *
 * Two columns: idle (deployed, no traffic) and the demo's load, a month in which the demo
 * account is seeded once (141 uploads) and 300 questions are asked.
 */

/** USD, from AWS's Price List API for us-east-1 (publication of 2026-10-19), except the NAT
 * gateway, whose price list (AmazonEC2, 480 MB) wasn't fetched: its price is from AWS's VPC
 * pricing page. */
export const PRICES = {
    fargateArmVcpuHour: 0.03238,
    fargateArmGbHour: 0.00356,
    fargateX86VcpuHour: 0.04048,
    fargateX86GbHour: 0.004445,
    natGatewayHour: 0.045,
    natGatewayGb: 0.045,
    publicIpv4Hour: 0.005,
    albHour: 0.0225,
    albLcuHour: 0.008,
    rdsT4gMicroHour: 0.016,
    rdsGp3GbMonth: 0.115,
    secretMonth: 0.4,
    alarmMetricMonth: 0.1,
    promqlAlarmMonth: 0.1,
    otelMetricsGb: 0.5,
    logsGb: 0.5,
    ecrGbMonth: 0.1
} as const;

const HOURS = 730;

/** Measured on 2026-10-23 by services/ai/evals/collector_check.py (run 20261023190612):
 * uncompressed OTLP bytes per scrape, idle; label values added by traffic grow them. */
export const BYTES_PER_SCRAPE: Record<string, number> = {
    api: 12_299,
    'ai-service': 4_808,
    'ingestion-worker': 3_772
};

/** The demo's load and other usage no template states. */
export const ASSUMPTIONS = {
    /** 141 uploads at 8-34 s each in the container (Stage 07), plus starts and the
     * ten idle minutes before scale-in. */
    workerHours: 1.5,
    /** Gemini: $0.0028 a question through the gateway (Stage 10b, A1 dev) and about
     * $0.0052 an upload (extraction $0.0019, contexts $0.0034; Stages 02 and 03). */
    geminiDemo: 300 * 0.0028 + 141 * 0.0052,
    /** NAT traffic: model calls, Qdrant, Langfuse, Hugging Face's model at TEI starts. */
    natGbIdle: 1,
    natGbDemo: 3,
    /** Log ingestion, all services. */
    logsGbIdle: 0.5,
    logsGbDemo: 1,
    /** ECR: the five images' compressed layers, a few versions kept. */
    ecrGb: 6,
    /** The ALB's capacity units: idle near zero. */
    lcuIdle: 0.05,
    lcuDemo: 0.2
} as const;

export interface CostRow {
    item: string;
    basis: string;
    idle: number;
    demo: number;
}

type Template = { Resources: Record<string, { Type: string; Properties?: any }> };

function all(templates: Template[], type: string) {
    return templates.flatMap((t) => Object.entries(t.Resources).filter(([, r]) => r.Type === type));
}

/** The rows of the estimate, from both stacks' synthesized templates. */
export function estimate(templates: Template[]): CostRow[] {
    const rows: CostRow[] = [];
    const tasks = Object.fromEntries(all(templates, 'AWS::ECS::TaskDefinition'));

    for (const [, service] of all(templates, 'AWS::ECS::Service')) {
        const name = String(service.Properties.ServiceName).replace(/^documents-/, '');
        const task = tasks[service.Properties.TaskDefinition.Ref].Properties;
        const vcpu = Number(task.Cpu) / 1024;
        const gb = Number(task.Memory) / 1024;
        const arm = task.RuntimePlatform.CpuArchitecture === 'ARM64';
        const hourly =
            vcpu * (arm ? PRICES.fargateArmVcpuHour : PRICES.fargateX86VcpuHour) +
            gb * (arm ? PRICES.fargateArmGbHour : PRICES.fargateX86GbHour);
        const always = service.Properties.DesiredCount * HOURS * hourly;
        const demo = name === 'ingestion-worker' ? ASSUMPTIONS.workerHours * hourly : always;
        rows.push({
            item: `Fargate: ${name}`,
            basis: `${vcpu} vCPU, ${gb} GB, ${arm ? 'Arm' : 'x86'}; ${
                name === 'ingestion-worker'
                    ? `0 tasks idle, ${ASSUMPTIONS.workerHours} task-hours under the demo`
                    : `${service.Properties.DesiredCount} task`
            }`,
            idle: always,
            demo
        });
    }

    const nats = all(templates, 'AWS::EC2::NatGateway').length;
    const ips = all(templates, 'AWS::EC2::EIP').length;
    rows.push({
        item: 'NAT gateway',
        basis: `${nats} gateway and ${ips} public IPv4 address; ${ASSUMPTIONS.natGbIdle} GB idle, ${ASSUMPTIONS.natGbDemo} GB under the demo`,
        idle:
            nats * HOURS * PRICES.natGatewayHour +
            ips * HOURS * PRICES.publicIpv4Hour +
            ASSUMPTIONS.natGbIdle * PRICES.natGatewayGb,
        demo:
            nats * HOURS * PRICES.natGatewayHour +
            ips * HOURS * PRICES.publicIpv4Hour +
            ASSUMPTIONS.natGbDemo * PRICES.natGatewayGb
    });

    const albs = all(templates, 'AWS::ElasticLoadBalancingV2::LoadBalancer').length;
    rows.push({
        item: 'Load balancer (internal)',
        basis: `${albs} ALB, no public address; CloudFront in front, within its free tier`,
        idle: albs * HOURS * (PRICES.albHour + ASSUMPTIONS.lcuIdle * PRICES.albLcuHour),
        demo: albs * HOURS * (PRICES.albHour + ASSUMPTIONS.lcuDemo * PRICES.albLcuHour)
    });

    for (const [, db] of all(templates, 'AWS::RDS::DBInstance')) {
        const storage = Number(db.Properties.AllocatedStorage);
        if (db.Properties.DBInstanceClass !== 'db.t4g.micro') {
            throw new Error(`no price for ${db.Properties.DBInstanceClass}`);
        }
        const monthly = HOURS * PRICES.rdsT4gMicroHour + storage * PRICES.rdsGp3GbMonth;
        rows.push({
            item: "RDS PostgreSQL (the gateway's)",
            basis: `${db.Properties.DBInstanceClass}, Single-AZ, ${storage} GB gp3`,
            idle: monthly,
            demo: monthly
        });
    }

    const secrets = all(templates, 'AWS::SecretsManager::Secret').length;
    rows.push({
        item: 'Secrets Manager',
        basis: `${secrets} secrets`,
        idle: secrets * PRICES.secretMonth,
        demo: secrets * PRICES.secretMonth
    });

    // Alarms: PromQL ones, metric alarms per metric (a math expression's each count), and
    // the two that every target-tracking policy creates.
    const alarms = all(templates, 'AWS::CloudWatch::Alarm');
    const promql = alarms.filter(([, a]) => a.Properties.EvaluationCriteria).length;
    const metricCount = alarms
        .filter(([, a]) => !a.Properties.EvaluationCriteria)
        .reduce(
            (n, [, a]) =>
                n +
                (a.Properties.Metrics
                    ? a.Properties.Metrics.filter((m: any) => m.MetricStat).length
                    : 1),
            0
        );
    const tracking = all(templates, 'AWS::ApplicationAutoScaling::ScalingPolicy').filter(
        ([, p]) => p.Properties.PolicyType === 'TargetTrackingScaling'
    ).length;
    const alarmCost =
        promql * PRICES.promqlAlarmMonth + (metricCount + 2 * tracking) * PRICES.alarmMetricMonth;
    rows.push({
        item: 'CloudWatch alarms',
        basis: `${promql} PromQL; ${metricCount} alarm metrics; ${2 * tracking} from target tracking`,
        idle: alarmCost,
        demo: alarmCost
    });

    const scrapesPerMonth = (HOURS * 3600) / 60;
    const always = BYTES_PER_SCRAPE.api + BYTES_PER_SCRAPE['ai-service'];
    const metricsGb = (bytes: number) => (bytes * scrapesPerMonth) / 1e9;
    const workerShare = (BYTES_PER_SCRAPE['ingestion-worker'] * ASSUMPTIONS.workerHours * 60) / 1e9;
    rows.push({
        item: 'CloudWatch metrics and logs',
        basis: `OTLP metrics, a scrape a minute (${metricsGb(always).toFixed(2)} GB); logs ${ASSUMPTIONS.logsGbIdle}-${ASSUMPTIONS.logsGbDemo} GB`,
        idle: metricsGb(always) * PRICES.otelMetricsGb + ASSUMPTIONS.logsGbIdle * PRICES.logsGb,
        demo:
            (metricsGb(always) + workerShare) * PRICES.otelMetricsGb +
            ASSUMPTIONS.logsGbDemo * PRICES.logsGb
    });

    rows.push({
        item: 'ECR',
        basis: `about ${ASSUMPTIONS.ecrGb} GB of image layers`,
        idle: ASSUMPTIONS.ecrGb * PRICES.ecrGbMonth,
        demo: ASSUMPTIONS.ecrGb * PRICES.ecrGbMonth
    });
    rows.push({
        item: 'S3, DynamoDB, SQS, SNS, CloudFront',
        basis: 'usage-priced; at the demo scale within free tiers or cents',
        idle: 0,
        demo: 0
    });
    return rows;
}

const usd = (n: number) => `$${n.toFixed(2)}`;

/** The estimate as README.md's table, Gemini (outside AWS) as a line of its own. */
export function markdown(rows: CostRow[]): string {
    const total = (key: 'idle' | 'demo') => rows.reduce((n, r) => n + r[key], 0);
    const lines = [
        '| Item | Basis | Idle month | Demo month |',
        '| --- | --- | --- | --- |',
        ...rows.map((r) => `| ${r.item} | ${r.basis} | ${usd(r.idle)} | ${usd(r.demo)} |`),
        `| **AWS total** | | **${usd(total('idle'))}** | **${usd(total('demo'))}** |`,
        `| Gemini, outside AWS | 300 questions, 141 uploads | $0.00 | ${usd(ASSUMPTIONS.geminiDemo)} |`
    ];
    return lines.join('\n');
}
