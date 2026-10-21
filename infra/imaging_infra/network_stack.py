"""Network: VPC with isolated subnets only (no internet gateway, no NAT), VPC endpoints, security groups.

All data-plane components (ECS tasks, Lambda functions, the OpenSearch domain) run in isolated
subnets and reach AWS services through VPC endpoints. Security groups are created here so the
search domain's ingress can name its callers without cross-stack cycles.
"""

from __future__ import annotations

from aws_cdk import Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_logs as logs
from constructs import Construct

from .common import log_retention, removal
from .config import AppConfig

INTERFACE_ENDPOINTS = {
    "Sqs": ec2.InterfaceVpcEndpointAwsService.SQS,
    "SageMakerRuntime": ec2.InterfaceVpcEndpointAwsService.SAGEMAKER_RUNTIME,
    "EcrApi": ec2.InterfaceVpcEndpointAwsService.ECR,
    "EcrDocker": ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
    "Logs": ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
}


class NetworkStack(Stack):
    def __init__(self, scope: Construct, cid: str, *, cfg: AppConfig, **kw) -> None:
        super().__init__(scope, cid, **kw)
        self.vpc = ec2.Vpc(
            self, "Vpc", vpc_name=cfg.name("vpc"), ip_addresses=ec2.IpAddresses.cidr(cfg.vpc_cidr),
            availability_zones=list(cfg.availability_zones), nat_gateways=0, restrict_default_security_group=True,
            subnet_configuration=[ec2.SubnetConfiguration(name="isolated", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                                                          cidr_mask=22)],
        )
        if cfg.flow_logs:
            group = logs.LogGroup(self, "FlowLogs", log_group_name=f"/{cfg.project}/vpc-flow-logs",
                                  retention=log_retention(cfg), removal_policy=removal(cfg))
            self.vpc.add_flow_log("FlowLog", destination=ec2.FlowLogDestination.to_cloud_watch_logs(group),
                                  traffic_type=ec2.FlowLogTrafficType.ALL)
        self.subnets = ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED)

        def sg(name: str, desc: str) -> ec2.SecurityGroup:
            return ec2.SecurityGroup(self, name, vpc=self.vpc, description=desc, allow_all_outbound=False,
                                     security_group_name=cfg.name(name.lower()))

        self.endpoint_sg = sg("Endpoints", "Interface VPC endpoints: HTTPS from the project security groups only")
        self.workers_sg = sg("Workers", "ECS dispatcher/indexer tasks: no ingress")
        self.api_sg = sg("Api", "Query/upload API Lambda: no ingress")
        self.bootstrap_sg = sg("SearchBootstrap", "Search bootstrap custom resource Lambda: no ingress")
        self.search_sg = sg("Search", "OpenSearch domain: HTTPS from the indexer, the API and the bootstrap only")
        self.vpc.add_gateway_endpoint("S3", service=ec2.GatewayVpcEndpointAwsService.S3, subnets=[self.subnets])
        self.vpc.add_gateway_endpoint("DynamoDb", service=ec2.GatewayVpcEndpointAwsService.DYNAMODB,
                                      subnets=[self.subnets])
        for client in (self.workers_sg, self.api_sg, self.bootstrap_sg):
            self.search_sg.add_ingress_rule(client, ec2.Port.tcp(443), "HTTPS from a known caller")
            client.add_egress_rule(self.search_sg, ec2.Port.tcp(443), "HTTPS to the search domain")
            client.add_egress_rule(self.endpoint_sg, ec2.Port.tcp(443), "HTTPS to interface endpoints")
            self.endpoint_sg.add_ingress_rule(client, ec2.Port.tcp(443), "HTTPS from a known caller")
        for client in (self.workers_sg, self.api_sg):
            # Gateway endpoints (S3, DynamoDB) are reached through route tables; the subnets have no route
            # to the internet, so this HTTPS egress can only reach the gateway endpoints and the VPC.
            client.add_egress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(443), "HTTPS to S3/DynamoDB gateway endpoints")
        services = dict(INTERFACE_ENDPOINTS)
        if cfg.api_endpoint_type == "PRIVATE":
            services["ExecuteApi"] = ec2.InterfaceVpcEndpointAwsService.APIGATEWAY
        self.endpoints: dict[str, ec2.InterfaceVpcEndpoint] = {}
        for name, svc in services.items():
            self.endpoints[name] = self.vpc.add_interface_endpoint(
                name, service=svc, subnets=self.subnets, security_groups=[self.endpoint_sg], private_dns_enabled=True,
                open=False)
