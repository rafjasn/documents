import { Validations } from 'aws-cdk-lib';
import { IConstruct } from 'constructs';
import { DocumentsInfraStack } from './infra-stack';
import { DocumentsServicesStack } from './services-stack';

/**
 * cdk-nag's AWS Solutions findings that were reviewed and accepted, each with its reason.
 * Everything else the checks raise fails `cdk synth` and the tests.
 */
export function acknowledgeReviewedFindings(
    infra: DocumentsInfraStack,
    services: DocumentsServicesStack
): void {
    const accept = (scope: IConstruct, id: string, reason: string) =>
        Validations.of(scope).acknowledge({ id, reason });

    // Edge
    const distribution = infra.distribution;
    accept(
        distribution,
        'AwsSolutions-CFR1',
        'No geo restriction: the demo has no regional audience to limit it to.'
    );
    accept(
        distribution,
        'AwsSolutions-CFR2',
        'No WAF: its web ACL costs more than the rest of the edge; sign-in and the budgets in the LLM gateway limit abuse. The first addition before real users.'
    );
    accept(
        distribution,
        'AwsSolutions-CFR4',
        "CloudFront's default certificate, for want of a domain, fixes the minimum TLS version; a custom domain's certificate would set TLSv1.2_2021."
    );
    accept(
        infra.node.findChild('Alb'),
        'AwsSolutions-ELB2',
        "The internal ALB is reached only through CloudFront's VPC origin, whose access logs record every request. ALB logs would also need the region at synth time, and the stacks are environment-agnostic."
    );

    // Queues and secrets
    accept(
        infra.notificationsQueue,
        'AwsSolutions-SQS3',
        'Status events for open browsers, kept 5 minutes; a missed one is shown when the document list next loads, so there is nothing to redrive.'
    );
    // The database's own secret (GatewayDb/Secret); `gatewayDb.secret` is its attachment.
    for (const secret of [
        ...Object.values(infra.secrets),
        infra.gatewayDb.node.findChild('Secret')
    ]) {
        accept(
            secret,
            'AwsSolutions-SMG4',
            "Third-party API keys are rotated at their provider and put here by hand; the services read secrets only at task start, so the database password's rotation would also need a restart of the gateway."
        );
    }

    // The gateway's database
    accept(
        infra.gatewayDb,
        'AwsSolutions-RDS3',
        "Single-AZ: it holds the gateway's keys and spend counters; restoring the daily backup loses at most a day's counts, and Multi-AZ would double its cost."
    );
    accept(
        infra.gatewayDb,
        'AwsSolutions-RDS11',
        "The default port: its security group admits only the gateway's tasks."
    );

    // The S3 notification's handler, a function CDK manages.
    for (const node of infra.node.children) {
        if (node.node.id.startsWith('BucketNotificationsHandler')) {
            accept(
                node,
                'AwsSolutions-IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole]',
                "CDK's bucket-notification handler, which runs only during deployments, logs through the AWS managed policy."
            );
        }
    }

    // Services
    accept(
        services.node.findChild('Cluster'),
        'AwsSolutions-ECS4',
        "Container Insights is off: about 160 custom metrics for six services, some $49 a month; ECS's own CPU and memory metrics per service are free, and the collectors send the services' metrics."
    );
    for (const { task } of Object.values(services.services)) {
        if (task.node.tryFindChild('ExecutionRole')) {
            accept(
                task.node.findChild('ExecutionRole'),
                'AwsSolutions-IAM5[Resource::*]',
                'ecr:GetAuthorizationToken has no resource-level permissions.'
            );
        }
        accept(
            task,
            'AwsSolutions-ECS2',
            'Only configuration is in environment variables; every key comes from Secrets Manager (tested).'
        );
    }
    for (const name of ['api', 'ai-service', 'ingestion-worker'] as const) {
        accept(
            services.services[name].task.taskRole,
            'AwsSolutions-IAM5[Resource::*]',
            'The collector sends metrics with cloudwatch:PutMetricData, which has no resource-level permissions.'
        );
    }
    // The bucket's ARN comes from the infra stack, so a finding names it by its export
    // once synthesized and by its attribute before.
    const bucketArn = [
        'DocumentsInfraStack:ExportsOutputFnGetAttDocumentsBucket9EC9DEB9ArnEA5095A7',
        '<DocumentsBucket9EC9DEB9.Arn>'
    ];
    const objects =
        "Objects under one prefix of the documents bucket, whose keys name the user and document; the tests pin each role's prefixes.";
    const prefixes: [IConstruct, string[]][] = [
        [services.services.api.task.taskRole, ['uploads', 'thumbnails']],
        [services.services['ingestion-worker'].task.taskRole, ['uploads', 'thumbnails', 'parsed']]
    ];
    for (const [role, granted] of prefixes) {
        for (const prefix of granted) {
            for (const arn of bucketArn) {
                accept(role, `AwsSolutions-IAM5[Resource::${arn}/${prefix}/*]`, objects);
            }
        }
    }
}
