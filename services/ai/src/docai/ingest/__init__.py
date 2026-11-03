"""The ingestion worker: parse uploads, caption figures, extract fields, store results.

The pipeline (pipeline.py) runs on local files without AWS; the worker (worker.py)
wraps it with the SQS queue, S3, DynamoDB and SNS.
"""
