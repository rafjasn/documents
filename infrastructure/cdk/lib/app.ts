import * as cdk from 'aws-cdk-lib';
import { AwsSolutionsChecks } from 'cdk-nag';
import { DeployConfig } from './config';
import { DocumentsInfraStack } from './infra-stack';
import { acknowledgeReviewedFindings } from './nag';
import { DocumentsServicesStack } from './services-stack';

export interface DocumentsApp {
    infra: DocumentsInfraStack;
    services: DocumentsServicesStack;
}

/** Both stacks with cdk-nag's AWS Solutions checks; shared by the CLI, tests and the cost estimate. */
export function buildApp(app: cdk.App, config: DeployConfig): DocumentsApp {
    const infra = new DocumentsInfraStack(app, 'DocumentsInfraStack', {
        description: 'Documents: network, storage, queues, edge, images, secrets',
        removalPolicy: config.removalPolicy,
        extraOrigins: config.extraOrigins
    });
    const services = new DocumentsServicesStack(app, 'DocumentsServicesStack', {
        description: 'Documents: the ECS Fargate services, scaling and alarms',
        infra,
        config
    });
    acknowledgeReviewedFindings(infra, services);
    cdk.Validations.of(app).addPlugins(new AwsSolutionsChecks(app, { verbose: true }));
    return { infra, services };
}
