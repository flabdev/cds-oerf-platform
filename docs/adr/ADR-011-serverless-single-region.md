# ADR-011: Serverless, single region, no VPC

**Status:** Accepted
**Date:** 2026-10-03
**Decider:** Architect

## Decision

The platform is Lambda, Step Functions, DynamoDB, S3, API Gateway, Cognito, and EventBridge. Nothing runs in a VPC. The region is us-east-1.

## Reasons

The data store choice ([ADR-003](ADR-003-dynamodb.md)) removed the reason to run a VPC. One region is enough for this release, and us-east-1 is where the messaging, Bedrock, and AgentCore pieces used here are deployed.

An early note left the region as us-east-1 or us-west-2, to be confirmed. The stacks use us-east-1.

## Consequences

There is no NAT gateway and no VPC endpoint. Cross-region recovery is out of scope ([ADR-018](README.md)). Account-specific features (WhatsApp account id, live channels, AgentCore, the desk stack) are CDK context flags so a synth without credentials still succeeds.
