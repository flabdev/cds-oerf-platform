# IAM exceptions

Actions below cannot be scoped to a resource ARN. Each one is limited to the role that needs it.

| Action | Role | Why |
| --- | --- | --- |
| `xray:PutTraceSegments`, `xray:PutTelemetryRecords` | `PreTokenFunctionRole`, `TenantIdAttributeFunctionRole` | X-Ray rejects resource-level permissions. `tracing=ACTIVE` on those functions adds both actions with `Resource: *`. |
