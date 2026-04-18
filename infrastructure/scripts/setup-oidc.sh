#!/bin/bash
set -euo pipefail

AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
AWS_REGION=${AWS_REGION:-us-east-1}
GITHUB_ORG=rafjasn
REPO_NAME=documents

echo "Setting up OIDC for ${GITHUB_ORG}/${REPO_NAME} in account ${AWS_ACCOUNT_ID}..."

aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1 \
  2>/dev/null || echo "OIDC provider already exists, skipping."

cat > /tmp/trust-policy.json << EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {
      "Federated": "arn:aws:iam::${AWS_ACCOUNT_ID}:oidc-provider/token.actions.githubusercontent.com"
    },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
      },
      "StringLike": {
        "token.actions.githubusercontent.com:sub": "repo:${GITHUB_ORG}/${REPO_NAME}:*"
      }
    }
  }]
}
EOF

aws iam create-role \
  --role-name documents-github-deploy \
  --assume-role-policy-document file:///tmp/trust-policy.json

aws iam attach-role-policy \
  --role-name documents-github-deploy \
  --policy-arn arn:aws:iam::aws:policy/AdministratorAccess

ROLE_ARN=$(aws iam get-role --role-name documents-github-deploy --query Role.Arn --output text)
echo ""
echo "Done. Add these to GitHub → Settings → Secrets:"
echo "  AWS_DEPLOY_ROLE_ARN = ${ROLE_ARN}"
echo "  AWS_ACCOUNT_ID      = ${AWS_ACCOUNT_ID}"
echo "  AWS_REGION          = ${AWS_REGION}"
echo ""
echo "Then run CDK bootstrap:"
echo "  npx cdk bootstrap aws://${AWS_ACCOUNT_ID}/${AWS_REGION}"