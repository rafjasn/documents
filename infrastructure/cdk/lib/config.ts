import * as cdk from 'aws-cdk-lib';

/**
 * What a deployment takes from outside the repository. The stacks are environment-agnostic:
 * the account and region come from the credentials at deploy time, so a synthesized
 * template names neither. Without CDK_REQUIRE_DEPLOY_CONFIG=true (set by deploy.yml) the
 * placeholders below stand in, so CI synthesizes and tests the stacks without AWS
 * credentials. Secrets never pass through here: they live in Secrets Manager.
 */
export interface DeployConfig {
    /** The tag of every image this repository builds (deploy.yml: the commit SHA). */
    imageTag: string;
    /** The Cognito user pool and its app client (a public client with USER_PASSWORD_AUTH). */
    cognitoUserPoolId: string;
    cognitoClientId: string;
    /** The Qdrant Cloud cluster; its API key is in Secrets Manager. */
    qdrantUrl: string;
    langfuseBaseUrl: string;
    /** Browser origins beside the CloudFront domain, such as a custom domain. */
    extraOrigins: string[];
    removalPolicy: cdk.RemovalPolicy;
}

const PLACEHOLDERS = {
    COGNITO_USER_POOL_ID: 'us-east-1_example',
    COGNITO_CLIENT_ID: 'exampleclientid1234567890',
    QDRANT_URL: 'https://example.cloud.qdrant.io:6333'
} as const;

export function deployConfig(app: cdk.App, env: NodeJS.ProcessEnv = process.env): DeployConfig {
    const required = env.CDK_REQUIRE_DEPLOY_CONFIG === 'true';
    const value = (name: keyof typeof PLACEHOLDERS): string => {
        const set = env[name]?.trim();
        if (set) {
            return set;
        }
        if (required) {
            throw new Error(`Missing ${name}: deploying needs it (see README.md, Deploying)`);
        }
        return PLACEHOLDERS[name];
    };
    const imageTag = app.node.tryGetContext('imageTag') as string | undefined;
    if (!imageTag && required) {
        throw new Error('Missing the imageTag context: deploy with -c imageTag=<commit SHA>');
    }
    return {
        imageTag: imageTag ?? 'synth',
        cognitoUserPoolId: value('COGNITO_USER_POOL_ID'),
        cognitoClientId: value('COGNITO_CLIENT_ID'),
        qdrantUrl: value('QDRANT_URL'),
        langfuseBaseUrl: env.LANGFUSE_BASE_URL?.trim() || 'https://cloud.langfuse.com',
        extraOrigins: (env.ALLOWED_ORIGINS ?? '')
            .split(',')
            .map((origin) => origin.trim())
            .filter(Boolean),
        // Stateful resources are kept when a stack is deleted, unless deployed with
        // -c removalPolicy=destroy (a throwaway environment).
        removalPolicy:
            app.node.tryGetContext('removalPolicy') === 'destroy'
                ? cdk.RemovalPolicy.DESTROY
                : cdk.RemovalPolicy.RETAIN
    };
}
