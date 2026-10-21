# Clean-up

Commands are in README "Clean-up". What they do and what you must check yourself:

1. `make destroy` asks for the prefix (or `CONFIRM_DESTROY=<PROJECT>`) and separately for data deletion
   (`CONFIRM_DATA=yes`). Each step warns and continues on failure:
   - saves the deployed asset manifests (`build/assets/`), then `cdk destroy --all`;
   - deletes ECS task-definition revisions of the project's families;
   - deletes log groups created by services outside the stacks: `/aws/sagemaker/Endpoints/<PREFIX>-endpoint`,
     `/aws/ecs/containerinsights/<PREFIX>-cluster/performance`, log groups of CDK helper Lambdas;
   - deletes the SageMaker AI lineage entities (contexts, actions, artifacts) that the service records automatically
     for each endpoint deployment; artifacts linked to anything outside this project are kept and reported;
   - deletes this project's objects (all versions) from the shared CDK bootstrap bucket, identified by asset hash
     and, for earlier deployments, by content (template with this project's tag value and stack prefix, or a zip
     with this project's handler); CDK provider packages shared with other projects and the CDKToolkit stack are not
     touched;
   - runs the residual check.
2. `make verify-cleanup` lists every category (stacks, buckets, log groups, ECR, Lambda, IAM roles/policies,
   SageMaker endpoints/endpoint configurations/models, scaling targets, OpenSearch domains, ECS clusters/task
   definitions, CodeBuild, VPCs, VPC endpoints, NAT gateways, security groups, ENIs, Elastic IPs, SQS, SNS, DynamoDB,
   alarms, dashboards, API Gateway, KMS aliases/keys, and every Project-tagged ARN confirmed in its own service) and
   exits non-zero if anything is left. The conclusion line reads "已清理，无残留" or "仍有残留：...".

## Not removed automatically

- **KMS keys** go to *PendingDeletion* (7-day window); they are reported separately and are the expected end state.
- **Retained data** in the handover profile (`removal_policy: retain`): buckets, the jobs table, the search domain,
  KMS keys and the ECR repository stay after `cdk destroy`. Delete them manually after exporting what you need.
- **CDK bootstrap stack and its shared assets** (CDKToolkit, provider packages) - shared with other projects.
- Objects in the bootstrap bucket from deployments of *other* commits are found only if their asset manifests are
  available (`build/assets/` or `--hash-list`); unidentified objects are not deleted and are reported.
- Lambda VPC network interfaces can take a while to be released after the functions are deleted; the residual check
  reports them until they are gone - re-run it later.

Do not assume a finished command means everything is gone: read the residual check's conclusion.
