# Prior art

Searched 2026-10-21: aws-samples and aws-solutions on GitHub (repository search for "sagemaker async",
"async inference autoscaling", "scale to zero", "sqs fargate autoscaling", "image search opensearch"),
the Amazon SageMaker AI developer guide, the AWS CDK API reference and AWS Solutions.

| Asset | What it covers | What it does not cover |
|---|---|---|
| [aws-samples/amazon-sagemaker-asynchronous-inference-computer-vision](https://github.com/aws-samples/amazon-sagemaker-asynchronous-inference-computer-vision) and its [blog post](https://aws.amazon.com/blogs/machine-learning/run-computer-vision-inference-on-large-videos-with-amazon-sagemaker-asynchronous-endpoints/) | PyTorch CV model on an asynchronous endpoint, SNS completion notifications, scaling on the backlog metric with a minimum of 0 instances | Notebook-driven, no IaC; no business-service layer in front of the endpoint; no metadata store or search; no retry/DLQ/idempotency design; no load-test method or fixed-capacity comparison |
| [aws/amazon-sagemaker-examples, async-inference](https://github.com/aws/amazon-sagemaker-examples/tree/main/async-inference) | Walkthrough notebooks for creating, invoking and autoscaling asynchronous endpoints | Same as above: single-layer (endpoint only), notebook form |
| [aws-samples/sample-kokoro-tts-sagemaker-endpoint](https://github.com/aws-samples/sample-kokoro-tts-sagemaker-endpoint) | One CloudFormation template for a scale-to-zero asynchronous endpoint (text-to-speech) | Image pipeline, queue in front of the endpoint, search, failure handling, evaluation method |
| [aws-samples/aws-fargate-pipeline-lambda-autoscaling](https://github.com/aws-samples/aws-fargate-pipeline-lambda-autoscaling) | S3 -> SQS -> Fargate workers for image pre-processing; a scheduled Lambda resizes the Fargate service from queue depth | Custom scaler instead of Application Auto Scaling; no managed inference layer; not maintained recently (last push 2020) |
| [CDK `QueueProcessingFargateService`](https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_ecs_patterns/QueueProcessingFargateService.html) | L3 construct: Fargate service with step scaling on `ApproximateNumberOfMessagesVisible` | A construct, not an application; no second scaling layer, no failure semantics beyond the queue's redrive policy |
| [EC2 Auto Scaling: scaling based on Amazon SQS](https://docs.aws.amazon.com/autoscaling/ec2/userguide/as-using-sqs-queue.html) | Method: backlog per instance as the scaling metric | Guidance only; EC2 Auto Scaling, not ECS or SageMaker |
| [Distributed Load Testing on AWS](https://aws.amazon.com/solutions/implementations/distributed-load-testing-on-aws/) ([repo](https://github.com/aws-solutions/distributed-load-testing-on-aws)) | Managed load generation against HTTP endpoints at scale | Our load is "objects arriving in a bucket", measured end to end through two queues and two scaling layers; DLT does not record queue wait or scaling activity timelines |
| [aws-samples/amazon-sagemaker-clip-search](https://github.com/aws-samples/amazon-sagemaker-clip-search), [Serverless semantic video search](https://github.com/aws-samples/Serverless-Semantic-Video-Search-Vector-Database-and-a-Multi-Modal-Generative-Al-Embeddings-Model) | Image/video search with OpenSearch and embeddings | Semantic (vector) search; no elastic batch-processing pipeline; no failure handling |

## Differences of this project

1. **Two scaling layers driven by queue depth, measured together.** ECS Fargate (ARM64) workers scale on
   the depth of the ingest queue and of the results queue (Application Auto Scaling step scaling); the
   SageMaker AI asynchronous endpoint scales on its own backlog metric with target tracking plus the
   documented scale-out-from-zero step policy. The load-test script records both layers' scaling
   activities, queue depth, queue wait and throughput on one timeline. None of the assets above
   combines the two layers or records such a timeline.
2. **A "fixed capacity vs elastic capacity" evaluation method** (what to measure, how to run the same
   load against both, how to read relative results: queue wait percentiles, utilisation, instance-time,
   cold-start contribution) with scripts, a local discrete-time simulation, and no price figures.
3. **Complete failure handling**: per-message retry with backoff, dead-letter queues on both queues,
   bounded re-invocation of failed or timed-out inferences, idempotent result writes (deterministic
   document IDs plus external versioning, conditional job-state transitions), and permanently bad files
   recorded as `rejected` without blocking the queue - all with tests and a cloud demonstration.
4. **Search on metadata and analysis-result fields** (structured filters, cursor pagination) in a
   VPC-only OpenSearch domain with fine-grained access control and an IAM master role, which the
   SageMaker samples do not include.

Conclusion: the assets cover single pieces (async endpoint scaling, queue workers, search). The
combination and the evaluation method are not covered, so development continues.
