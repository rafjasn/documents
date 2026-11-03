#!/bin/bash
# The role .github/workflows/deploy.yml assumes through GitHub's OIDC provider. Not run in
# this project (the stacks aren't deployed). Run once per account, with administrator
# credentials, after `npx cdk bootstrap`.
#
# The role can do two things only: assume the CDK bootstrap roles, which deploy the
# stacks, and push to the five image repositories. Only workflows on main can assume it.
set -euo pipefail

AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
AWS_REGION=${AWS_REGION:-us-east-1}
GITHUB_ORG=${GITHUB_ORG:-rafjasn}
REPO_NAME=${REPO_NAME:-documents}
ROLE_NAME=documents-github-deploy
policy_dir=$(mktemp -d)
trap 'rm -rf "$policy_dir"' EXIT

echo "Setting up OIDC for ${GITHUB_ORG}/${REPO_NAME}..."

aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  2>/dev/null || echo "OIDC provider already exists, skipping."

cat > "$policy_dir/trust.json" << EOF
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
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
        "token.actions.githubusercontent.com:sub": "repo:${GITHUB_ORG}/${REPO_NAME}:ref:refs/heads/main"
      }
    }
  }]
}
EOF

cat > "$policy_dir/deploy.json" << EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "CdkBootstrapRoles",
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Resource": "arn:aws:iam::${AWS_ACCOUNT_ID}:role/cdk-*-${AWS_ACCOUNT_ID}-${AWS_REGION}"
    },
    {
      "Sid": "EcrLogin",
      "Effect": "Allow",
      "Action": "ecr:GetAuthorizationToken",
      "Resource": "*"
    },
    {
      "Sid": "PushImages",
      "Effect": "Allow",
      "Action": [
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchGetImage",
        "ecr:CompleteLayerUpload",
        "ecr:InitiateLayerUpload",
        "ecr:PutImage",
        "ecr:UploadLayerPart"
      ],
      "Resource": "arn:aws:ecr:${AWS_REGION}:${AWS_ACCOUNT_ID}:repository/documents/*"
    }
  ]
}
EOF

aws iam create-role \
  --role-name "$ROLE_NAME" \
  --assume-role-policy-document "file://$policy_dir/trust.json"

aws iam put-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name deploy \
  --policy-document "file://$policy_dir/deploy.json"

ROLE_ARN=$(aws iam get-role --role-name "$ROLE_NAME" --query Role.Arn --output text)
echo ""
echo "Done. In GitHub → Settings → Secrets and variables → Actions, add:"
echo "  secret   AWS_DEPLOY_ROLE_ARN = ${ROLE_ARN}"
echo "  variable AWS_REGION          = ${AWS_REGION}"
echo "and the variables deploy.yml lists (Cognito, Qdrant, Langfuse)."
