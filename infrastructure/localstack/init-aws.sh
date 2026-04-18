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

echo "Creating thumbnail DLQ..."
awslocal sqs create-queue \
  --queue-name documents-thumbnail-dlq \
  --region $REGION

THUMB_DLQ_ARN=$(awslocal sqs get-queue-attributes \
  --queue-url http://sqs.us-east-1.localhost.localstack.cloud:4566/000000000000/documents-thumbnail-dlq \
  --attribute-names QueueArn \
  --query 'Attributes.QueueArn' \
  --output text --region $REGION)

echo "Creating thumbnail jobs queue..."
awslocal sqs create-queue \
  --queue-name documents-thumbnail-jobs \
  --attributes "{
    \"RedrivePolicy\": \"{\\\"deadLetterTargetArn\\\":\\\"${THUMB_DLQ_ARN}\\\",\\\"maxReceiveCount\\\":\\\"3\\\"}\",
    \"VisibilityTimeout\": \"120\"
  }" \
  --region $REGION

echo "Configuring S3 → SQS event notification..."
awslocal s3api put-bucket-notification-configuration \
  --bucket documents-uploads \
  --notification-configuration "{
    \"QueueConfigurations\": [{
      \"QueueArn\": \"${PROC_QUEUE_ARN}\",
      \"Events\": [\"s3:ObjectCreated:*\"],
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

echo "Creating DynamoDB table..."
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

echo "Creating CloudWatch log groups..."
for svc in api worker ai; do
  awslocal logs create-log-group \
    --log-group-name "/documents/${svc}" \
    --region $REGION
done

echo "Creating CloudWatch metric filters..."
for svc in api worker ai; do
  svc_title="$(tr '[:lower:]' '[:upper:]' <<< "${svc:0:1}")${svc:1}"

  awslocal logs put-metric-filter \
    --log-group-name "/documents/${svc}" \
    --filter-name "${svc}-errors" \
    --filter-pattern '{ $.level = "ERROR" }' \
    --metric-transformations \
      "metricName=${svc_title}ErrorCount,metricNamespace=Documents,metricValue=1,defaultValue=0" \
    --region $REGION

  awslocal logs put-metric-filter \
    --log-group-name "/documents/${svc}" \
    --filter-name "${svc}-fatals" \
    --filter-pattern '{ $.level = "FATAL" }' \
    --metric-transformations \
      "metricName=${svc_title}FatalCount,metricNamespace=Documents,metricValue=1,defaultValue=0" \
    --region $REGION
done

echo "Creating alerts SNS topic..."
awslocal sns create-topic \
  --name documents-alerts \
  --region $REGION

ALERTS_TOPIC_ARN=$(awslocal sns list-topics \
  --region $REGION \
  --query 'Topics[?ends_with(TopicArn, `:documents-alerts`)].TopicArn' \
  --output text)

echo "Creating CloudWatch alarms..."
for svc in api worker ai; do
  svc_title="$(tr '[:lower:]' '[:upper:]' <<< "${svc:0:1}")${svc:1}"

  awslocal cloudwatch put-metric-alarm \
    --alarm-name "documents-${svc}-errors" \
    --alarm-description "More than 10 ERROR logs in 5 minutes from ${svc}" \
    --metric-name "${svc_title}ErrorCount" \
    --namespace Documents \
    --statistic Sum \
    --period 300 \
    --threshold 10 \
    --comparison-operator GreaterThanThreshold \
    --evaluation-periods 1 \
    --treat-missing-data notBreaching \
    --alarm-actions "$ALERTS_TOPIC_ARN" \
    --region $REGION

  awslocal cloudwatch put-metric-alarm \
    --alarm-name "documents-${svc}-fatal" \
    --alarm-description "Any FATAL log from ${svc}" \
    --metric-name "${svc_title}FatalCount" \
    --namespace Documents \
    --statistic Sum \
    --period 60 \
    --threshold 0 \
    --comparison-operator GreaterThanThreshold \
    --evaluation-periods 1 \
    --treat-missing-data notBreaching \
    --alarm-actions "$ALERTS_TOPIC_ARN" \
    --region $REGION
done

for queue_name in documents-dlq documents-thumbnail-dlq; do
  alarm_name="$(echo "$queue_name" | sed 's/documents-//' | sed 's/-dlq//')-dlq-depth"
  awslocal cloudwatch put-metric-alarm \
    --alarm-name "documents-${alarm_name}" \
    --alarm-description "Messages visible in ${queue_name}" \
    --metric-name "ApproximateNumberOfMessagesVisible" \
    --namespace "AWS/SQS" \
    --dimensions "Name=QueueName,Value=${queue_name}" \
    --statistic Maximum \
    --period 300 \
    --threshold 0 \
    --comparison-operator GreaterThanThreshold \
    --evaluation-periods 1 \
    --treat-missing-data notBreaching \
    --alarm-actions "$ALERTS_TOPIC_ARN" \
    --region $REGION
done

echo ""
echo "  All resources provisioned!"
echo ""
echo "  S3:         s3://documents-uploads"
echo "  SQS:        documents-processing      (DLQ: documents-dlq)"
echo "  SQS:        documents-thumbnail-jobs  (DLQ: documents-thumbnail-dlq)"
echo "  SQS:        documents-api-notifications (subscribed to SNS)"
echo "  DynamoDB:   documents-documents   (PK/SK + GSI1)"
echo "  SNS:        documents-notifications"
echo "  SNS:        documents-alerts"
echo "  CW Logs:    /documents/api  /documents/worker  /documents/ai"
echo "  CW Alarms:  error-rate (per service) + DLQ depth"
