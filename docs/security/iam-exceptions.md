# IAM exceptions

Actions below cannot be scoped to a resource ARN, or are waiting on a later ticket to name the ARN. Each one is limited to the role that needs it.

| Action | Role | Why |
| --- | --- | --- |
| `xray:PutTraceSegments`, `xray:PutTelemetryRecords` | `PreTokenFunctionRole`, `TenantIdAttributeFunctionRole` | X-Ray rejects resource-level permissions. `tracing=ACTIVE` on those functions adds both actions with `Resource: *`. |
| `xray:PutTraceSegments`, `xray:PutTelemetryRecords` | `ChannelRouterFunctionRole`, `EvidenceAnalyzerFunctionRole`, `ReportGeneratorFunctionRole`, `WsHandlerFunctionRole`, `WsAuthorizerFunctionRole`, `StreamBroadcasterFunctionRole`, `ActionHandlerFunctionRole`, `InboundFunctionRole`, `IngestFunctionRole` | `tracing=ACTIVE` on every compute function adds both actions with `Resource: *`. X-Ray rejects a resource ARN. |
| `states:SendTaskSuccess`, `states:SendTaskFailure` | `InboundFunctionRole`. Also `ActionHandlerFunctionRole` while context `compat:actionHandlerTaskToken` is true (default). | Task-token APIs have no resource type. OERF-208 still calls `SendTaskSuccess` from the action handler. Set the flag to false once that handler sends to the inbound queue. |
| `sns:Publish` | `ChannelRouterFunctionRole` | Publishing SMS to a phone number has no SNS resource ARN. SMS should move to `pinpoint-sms-voice-v2` under OERF-116 (Mudita). |
| `dynamodb:ListStreams` | `StreamBroadcasterFunctionRole` | The incident-stream event source calls `ListStreams`, which has no resource type. `DescribeStream`, `GetRecords`, and `GetShardIterator` stay on this table's stream ARN. |
| `social-messaging:SendWhatsAppMessage` | `ChannelRouterFunctionRole` | TODO: when context `whatsapp:phoneNumberId` is unset, no phone-number-id ARN exists at synth, so the resource stays `*`. When the id is set, the statement is `arn:aws:social-messaging:{region}:{account}:phone-number-id/{id}` and this row does not apply. |
| `xray:PutTraceSegments`, `xray:PutTelemetryRecords` | `AgentRuntimeExecutionRole` | X-Ray rejects a resource ARN. The AgentCore runtime enables tracing on this role only when context `agentcore:enabled` is true. |
| `ecr:GetAuthorizationToken` | `AgentRuntimeExecutionRole` | ECR authorization has no resource type. Image pull actions stay on the asset repository ARN. Present only when context `agentcore:enabled` is true. |
| `xray:GetSamplingRules`, `xray:GetSamplingTargets` | `AgentRuntimeExecutionRole` | X-Ray sampling APIs have no resource type. The AgentCore runtime adds them when tracing is enabled. |
| `cloudwatch:PutMetricData` | `AgentRuntimeExecutionRole` | `PutMetricData` has no resource type. The AgentCore runtime adds it when tracing is enabled. |
| `xray:PutTraceSegments`, `xray:PutTelemetryRecords` | role of `oerf-waba-event-destination` | `tracing=ACTIVE` on that function. Present only when context `whatsapp:wabaId` is set. X-Ray rejects a resource ARN. |
