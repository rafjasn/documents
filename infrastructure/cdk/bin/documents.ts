#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { DocumentsInfraStack } from '../lib/infra-stack';
import { DocumentsServicesStack } from '../lib/services-stack';

const app = new cdk.App();

const env: cdk.Environment = {
    account: process.env.CDK_DEFAULT_ACCOUNT ?? process.env.AWS_ACCOUNT_ID,
    region: process.env.CDK_DEFAULT_REGION ?? process.env.AWS_REGION ?? 'us-east-1'
};

// Frontend origin(s) allowed to upload/download directly to S3 via presigned URLs.
// Set via env var, e.g.:
//   ALLOWED_ORIGINS=https://app.example.com,https://documents-xxx.cloudfront.net cdk deploy
const allowedOrigins = (process.env.ALLOWED_ORIGINS ?? '')
    .split(',')
    .map((o) => o.trim())
    .filter(Boolean);

if (allowedOrigins.length === 0) {
    throw new Error(
        'Missing ALLOWED_ORIGINS env var (comma-separated origins). ' +
            'Provide the frontend ALB DNS, CloudFront domain, or custom domain.'
    );
}

const infra = new DocumentsInfraStack(app, 'DocumentsInfraStack', {
    env,
    description: 'Documents app',
    // Protect stateful resources from accidental deletion in production.
    // Set to DESTROY for dev environments:
    //   cdk deploy --context removalPolicy=destroy
    removalPolicy:
        app.node.tryGetContext('removalPolicy') === 'destroy'
            ? cdk.RemovalPolicy.DESTROY
            : cdk.RemovalPolicy.RETAIN,
    allowedOrigins
});

new DocumentsServicesStack(app, 'DocumentsServicesStack', {
    env,
    description: 'Documents app — ECS Fargate services and ALB',
    infra,
    imageTag: app.node.tryGetContext('imageTag') ?? 'latest'
});
