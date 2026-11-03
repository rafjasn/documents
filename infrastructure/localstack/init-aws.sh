#!/bin/bash
set -euo pipefail

echo "Provisioning AWS resources in LocalStack..."

REGION="us-east-1"

echo "Creating S3 bucket..."
awslocal s3 mb s3://documents-uploads --region $REGION

echo "Configuring S3 CORS..."
awslocal s3api put-bucket-cors \
  --bucket documents-uploads \
  --cors-configuration '{
    "CORSRules": [{
      "AllowedOrigins": ["*"],
      "AllowedMethods": ["GET", "PUT", "POST"],
      "AllowedHeaders": ["*"],
      "MaxAgeSeconds": 3600
    }]
  }' \
  --region $REGION

echo "Creating processing DLQ..."
awslocal sqs create-queue \
  --queue-name documents-dlq \
  --region $REGION

DLQ_ARN=$(awslocal sqs get-queue-attributes \
  --queue-url http://sqs.us-east-1.localhost.localstack.cloud:4566/000000000000/documents-dlq \
  --attribute-names QueueArn \
  --query 'Attributes.QueueArn' \
  --output text --region $REGION)

echo "Creating processing queue..."
awslocal sqs create-queue \
  --queue-name documents-processing \
  --attributes "{
    \"RedrivePolicy\": \"{\\\"deadLetterTargetArn\\\":\\\"${DLQ_ARN}\\\",\\\"maxReceiveCount\\\":\\\"3\\\"}\",
    \"VisibilityTimeout\": \"120\"
  }" \
  --region $REGION

PROC_QUEUE_ARN=$(awslocal sqs get-queue-attributes \
  --queue-url http://sqs.us-east-1.localhost.localstack.cloud:4566/000000000000/documents-processing \
  --attribute-names QueueArn \
  --query 'Attributes.QueueArn' \
  --output text --region $REGION)

echo "Configuring S3 → SQS event notification..."
awslocal s3api put-bucket-notification-configuration \
  --bucket documents-uploads \
  --notification-configuration "{
    \"QueueConfigurations\": [{
      \"QueueArn\": \"${PROC_QUEUE_ARN}\",
      \"Events\": [\"s3:ObjectCreated:*\", \"s3:ObjectRemoved:*\"],
      \"Filter\": {
        \"Key\": {
          \"FilterRules\": [{
            \"Name\": \"prefix\",
            \"Value\": \"uploads/\"
          }]
        }
      }
    }]
  }" \
  --region $REGION

echo "Creating SNS topic..."
awslocal sns create-topic \
  --name documents-notifications \
  --region $REGION

SNS_TOPIC_ARN=$(awslocal sns list-topics \
  --region $REGION \
  --query 'Topics[?ends_with(TopicArn, `:documents-notifications`)].TopicArn' \
  --output text)

echo "Creating API notifications queue..."
awslocal sqs create-queue \
  --queue-name documents-api-notifications \
  --region $REGION

API_NOTIF_QUEUE_ARN=$(awslocal sqs get-queue-attributes \
  --queue-url http://sqs.us-east-1.localhost.localstack.cloud:4566/000000000000/documents-api-notifications \
  --attribute-names QueueArn \
  --query 'Attributes.QueueArn' \
  --output text --region $REGION)

echo "Subscribing API notifications queue to SNS topic..."
awslocal sns subscribe \
  --topic-arn "$SNS_TOPIC_ARN" \
  --protocol sqs \
  --notification-endpoint "$API_NOTIF_QUEUE_ARN" \
  --region $REGION

# DynamoDB data survives a LocalStack restart (S3 objects don't), so the table may exist.
echo "Creating DynamoDB table..."
awslocal dynamodb describe-table --table-name documents-documents --region $REGION >/dev/null 2>&1 ||
awslocal dynamodb create-table \
  --table-name documents-documents \
  --attribute-definitions \
    AttributeName=PK,AttributeType=S \
    AttributeName=SK,AttributeType=S \
    AttributeName=GSI1PK,AttributeType=S \
    AttributeName=GSI1SK,AttributeType=S \
  --key-schema \
    AttributeName=PK,KeyType=HASH \
    AttributeName=SK,KeyType=RANGE \
  --global-secondary-indexes '[
    {
      "IndexName": "GSI1",
      "KeySchema": [
        {"AttributeName": "GSI1PK", "KeyType": "HASH"},
        {"AttributeName": "GSI1SK", "KeyType": "RANGE"}
      ],
      "Projection": {"ProjectionType": "ALL"},
      "ProvisionedThroughput": {"ReadCapacityUnits": 5, "WriteCapacityUnits": 5}
    }
  ]' \
  --provisioned-throughput ReadCapacityUnits=5,WriteCapacityUnits=5 \
  --region $REGION

echo ""
echo "  All resources provisioned!"
echo ""
echo "  S3:         s3://documents-uploads"
echo "  SQS:        documents-processing      (DLQ: documents-dlq)"
echo "  SQS:        documents-api-notifications (subscribed to SNS)"
echo "  DynamoDB:   documents-documents   (PK/SK + GSI1)"
echo "  SNS:        documents-notifications"
