import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cloudwatchActions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import { Construct } from 'constructs';

/**
 * The Stage 10c alert rules (infrastructure/grafana/provisioning/alerting/rules.yaml) as
 * CloudWatch alarms. Latency, errors and spend are PromQL alarms over the metrics the
 * collectors send, with the same expressions, windows and pending periods as Grafana's
 * rules; the histogram is a native one in CloudWatch, so it needs no `le`. The two queue
 * rules read SQS's own metrics instead, which LocalStack didn't provide: the queue's depth,
 * and the age of its oldest message. Every alarm notifies the alerts topic.
 */
export interface PromQlRule {
    id: string;
    name: string;
    description: string;
    query: string;
    pendingSeconds: number;
}

const FAILED = '(sum(increase(docai_chat_requests_total{outcome="failed"}[5m])) or vector(0))';
const FAILED_UPLOADS = '(sum(increase(docai_documents_total{status="failed"}[5m])) or vector(0))';
const CHATS = '(sum(increase(docai_chat_requests_total[5m])) or vector(0))';
const UPLOADS = '(sum(increase(docai_documents_total[5m])) or vector(0))';

export const PROMQL_RULES: PromQlRule[] = [
    {
        id: 'chat-latency-p95',
        name: 'documents-chat-latency-p95',
        description: 'Chat p95 latency above 15 s for 5 minutes',
        query: 'histogram_quantile(0.95, sum(rate(docai_chat_seconds[10m]))) > 15',
        pendingSeconds: 300
    },
    {
        id: 'error-rate',
        name: 'documents-error-rate',
        description:
            'Chat and upload error rate above 20%; refusals over a budget are the gateway working, not failures',
        query: `(${FAILED} + ${FAILED_UPLOADS}) / (${CHATS} + ${UPLOADS}) > 0.2`,
        pendingSeconds: 60
    },
    {
        id: 'hourly-spend',
        name: 'documents-hourly-spend',
        description: 'Model spend above $1 an hour',
        query: 'sum(increase(docai_llm_cost_usd_total[1h])) > 1',
        pendingSeconds: 0
    }
];

export interface AlarmsProps {
    alertsTopic: sns.ITopic;
    processingQueue: sqs.IQueue;
    processingDlq: sqs.IQueue;
}

export class Alarms extends Construct {
    constructor(scope: Construct, id: string, props: AlarmsProps) {
        super(scope, id);
        const action = new cloudwatchActions.SnsAction(props.alertsTopic);

        for (const rule of PROMQL_RULES) {
            new cloudwatch.CfnAlarm(this, rule.id, {
                alarmName: rule.name,
                alarmDescription: rule.description,
                evaluationCriteria: {
                    promQlCriteria: {
                        query: rule.query,
                        pendingPeriod: rule.pendingSeconds,
                        recoveryPeriod: 60
                    }
                },
                evaluationInterval: 60,
                alarmActions: [props.alertsTopic.topicArn],
                okActions: [props.alertsTopic.topicArn]
            });
        }

        const minute = cdk.Duration.minutes(1);
        const sqsAlarms: [string, string, cloudwatch.IMetric, number, number][] = [
            [
                'queue-held-up',
                'Uploads waiting on the queue for 10 minutes',
                props.processingQueue.metricApproximateNumberOfMessagesVisible({
                    period: minute,
                    statistic: 'Maximum'
                }),
                0,
                10
            ],
            [
                'messages-old',
                'Uploads older than 5 minutes on the queue',
                props.processingQueue.metricApproximateAgeOfOldestMessage({
                    period: minute,
                    statistic: 'Maximum'
                }),
                300,
                5
            ],
            [
                'dlq-depth',
                'Uploads in the dead-letter queue: they failed three times',
                props.processingDlq.metricApproximateNumberOfMessagesVisible({
                    period: cdk.Duration.minutes(5),
                    statistic: 'Maximum'
                }),
                0,
                1
            ]
        ];
        for (const [name, description, metric, threshold, periods] of sqsAlarms) {
            const alarm = new cloudwatch.Alarm(this, name, {
                alarmName: `documents-${name}`,
                alarmDescription: description,
                metric,
                threshold,
                evaluationPeriods: periods,
                comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING
            });
            alarm.addAlarmAction(action);
            alarm.addOkAction(action);
        }
    }
}
