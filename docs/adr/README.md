# Architecture Decision Records

Decisions for the MVP. A new decision gets a new ADR. A superseded ADR stays, with a pointer to the one that replaced it.

A **file** link is the full record. **Summary only** means the one-line decision below is the public record. Where an early note disagrees with the code, the code wins, and the ADR says so.

| ADR | Title | Status | Decision | Record |
| --- | --- | --- | --- | --- |
| 001 | Problem statement: OERF, cold-chain first | Accepted | Build the Operational Exception Resolution Framework, and ship the cold-chain excursion playbook first. | [file](ADR-001-problem-statement.md) |
| 002 | Pooled multi-tenancy | Accepted | One deployment. `tenant_id` is on every key, object prefix, log line, and execution name. | [file](ADR-002-pooled-multi-tenancy.md) |
| 003 | DynamoDB as the data store | Accepted | Four on-demand tables: platform, tenant, incident, and routing. | [file](ADR-003-dynamodb.md) |
| 004 | Step Functions Standard as the orchestrator | Accepted | One Standard state machine per playbook. Task tokens wait for people. Engagement is a nested machine. | [file](ADR-004-step-functions.md) |
| 005 | Engagement primitive | Accepted | A reusable engagement workflow, driven by tenant cadence policies and communication profiles. | [file](ADR-005-engagement-primitive.md) |
| 006 | Evidence processing | Accepted | Bedrock vision reads photos in the MVP. Document AI and transcription stay out of this release. | [file](ADR-006-evidence-processing.md) |
| 007 | Agent boundaries | Accepted | AgentCore advises. Rules decide. The agent cannot send or change incident state. | [file](ADR-007-agent-advisory.md) |
| 008 | Single channel router | Accepted | Only the channel router sends WhatsApp, SMS, and email. WhatsApp goes through End User Messaging Social. | [file](ADR-008-channel-router.md) |
| 009 | Thin admin portal | Accepted | Settings UI was to be generated from domain JSON Schema. The deployed desk publishes an allowance only. | [file](ADR-009-admin-portal.md) |
| 010 | Live updates | Accepted | The incident table stream pushes tenant-scoped updates over API Gateway WebSocket. | [file](ADR-010-live-updates.md) |
| 011 | Serverless, single region, no VPC | Accepted | Lambda, Step Functions, DynamoDB, S3, API Gateway, Cognito, and EventBridge in us-east-1, with no VPC. | [file](ADR-011-serverless-single-region.md) |
| 012 | Simulate external systems | Accepted | A deterministic simulator and seed data stand in for sensors and transport systems. | [file](ADR-012-simulated-systems.md) |
| 013 | Config snapshot per incident | Accepted | Opening an incident pins the config version that its rules will use. | [file](ADR-013-config-snapshot.md) |
| 014 | Voice rung is a one-way attention call | Accepted | A voice rung tells the person to reply on WhatsApp. The router does not place a call. | summary only |
| 015 | Channel two-way matrix | Accepted | WhatsApp is two-way. Email can carry a reply. SMS and voice do not resume the workflow. | summary only |
| 016 | One open incident per shipment | Accepted | A lock item, written with the incident, allows one open incident per tenant and shipment. | summary only |
| 017 | Reply classification once | Accepted | Inbound dedupes and resumes. Interpret classifies. Buttons skip the agent. | summary only |
| 018 | Data retention and recovery | Accepted | Evidence content expires after 90 days. Point-in-time recovery is on. No cross-region recovery. | summary only |
| 019 | Manual incident cancellation | Accepted | An operator cancels from the desk. Inbound calls `StopExecution` and the repository releases the lock. | [file](ADR-019-manual-cancellation.md) |
| 020 | Bedrock model is Amazon Nova Lite | Accepted | Vision and the agent use `amazon.nova-lite-v1:0`. There is no fallback model in code. | [file](ADR-020-bedrock-model.md) |
| 021 | Report rendering | Accepted | A report renders as HTML, and as PDF when the WeasyPrint backend is selected. | summary only |
| 022 | Repository visibility | Accepted | The repository is public and licensed under MIT. | summary only |
| 023 | Sandbox tenant | Accepted | A separate sandbox tenant accepts a join from a number that no tenant has registered. | summary only |
| 024 | CALL ME is a human callback | Accepted | CALL ME asks a configured role to phone the person back. The platform does not hold a voice conversation. | summary only |
| 025 | Cadence per role and ask | Accepted | The config maps `role.ask` to a cadence policy. This amends ADR-005. | summary only |
| 026 | Product profiles only in config | Accepted | Product profiles exist only in the published domain config. | summary only |
| 027 | Monitoring until closed | Accepted | After containment, monitoring continues until the incident is closed. A rise can reopen it. | summary only |
| 028 | Dashboard actions through inbound | Accepted | Desk actions are queued to inbound. Only inbound resumes a waiting task token. | summary only |
| 029 | Phone registry | Accepted | The routing table keeps a permanent row for each registered address. | summary only |
| 030 | Callback wait and exhaustion | Accepted | A callback pause does not move the exhaustion deadline. This amends ADR-024. | summary only |
| 031 | Reopen during QA | Accepted | A reopen stops the QA engagement and starts it again after the shipment is contained. This amends ADR-027. | summary only |
| 032 | Sandbox join cap | Accepted | Sandbox self-registration has a daily cap. This amends ADR-023. | summary only |
| 033 | Encryption at rest | Accepted | DynamoDB, S3, and SQS use AWS managed encryption. There is no customer-managed key in the MVP. | [file](ADR-033-encryption-at-rest.md) |
| 034 | Cognito tenant claim | Accepted | `custom:tenant_id` is added on the existing user pool, and a pre-token trigger copies it onto the access token. | [file](ADR-034-cognito-tenant-claim.md) |
| 035 | Incident list by status | Accepted | GSI1 lists one tenant's incidents in one state, ordered by open time. | [file](ADR-035-incident-gsi1.md) |
| 036 | State machine tracing | Accepted | Both playbook state machines export X-Ray traces. Execution data is not logged. | [file](ADR-036-state-machine-tracing.md) |
