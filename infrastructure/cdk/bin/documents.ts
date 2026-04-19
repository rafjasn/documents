#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { DocumentsInfraStack } from '../lib/infra-stack';
import { DocumentsServicesStack } from '../lib/services-stack';

const repoRootEnvPath = resolve(__dirname, '../../../.env');
const requireDeployAuthEnv = process.env.CDK_REQUIRE_AUTH_ENV === 'true';
const synthDefaults = {
    COGNITO_USER_POOL_ID: 'us-east-1_example',
    COGNITO_CLIENT_ID: 'exampleclientid1234567890'
} as const;

loadRepoEnv(repoRootEnvPath);

const app = new cdk.App();

function requiredEnv(name: string): string {
    const value = process.env[name]?.trim();

    if (value) {
        return value;
    }

    if (!requireDeployAuthEnv && name in synthDefaults) {
        return synthDefaults[name as keyof typeof synthDefaults];
    }

    throw new Error(`Missing ${name} env var.`);
}

function loadRepoEnv(envPath: string): void {
    if (!existsSync(envPath)) {
        return;
    }

    const lines = readFileSync(envPath, 'utf8').split(/\r?\n/);

    for (const rawLine of lines) {
        const trimmed = rawLine.trim();

        if (!trimmed || trimmed.startsWith('#')) {
            continue;
        }

        const line = trimmed.startsWith('export ') ? trimmed.slice(7) : trimmed;
        const equalsIndex = line.indexOf('=');

        if (equalsIndex === -1) {
            continue;
        }

        const key = line.slice(0, equalsIndex).trim();

        if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key) || process.env[key] !== undefined) {
            continue;
        }

        let value = line.slice(equalsIndex + 1).trim();

        if (
            (value.startsWith('"') && value.endsWith('"')) ||
            (value.startsWith("'") && value.endsWith("'"))
        ) {
            value = value.slice(1, -1);
        }

        process.env[key] = value.replace(/\\n/g, '\n');
    }
}

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
    imageTag: app.node.tryGetContext('imageTag') ?? 'latest',
    auth: {
        cognitoUserPoolId: requiredEnv('COGNITO_USER_POOL_ID'),
        cognitoClientId: requiredEnv('COGNITO_CLIENT_ID')
    }
});
