# ADR-037: Stage-aware deployments (main and dev in one account)

- **Status:** Accepted
- **Date:** 2026-11-11
- **Tracker:** A1, A2, A3, A6, G4

## Context

After the hackathon submission on 28 Oct, `main` had to stay frozen while the team kept building. A second copy of the platform was needed for development and rehearsals. Every stack name and most physical names were fixed (`OerfDataStack`, `oerf-cold-chain`, `/oerf/...`), some resources can exist only once per account (the GitHub OIDC provider, the X-Ray Transaction Search setting, the API Gateway account role), and the CDK bootstrap roles were shared. Deploying the same code a second time would either clash on names or overwrite main.

A separate AWS account was not available in the time frame, and the WhatsApp Business Account (one number, one event destination) is in this account.

## Decision

1. **One CDK app, two stages.** `-c stage=main|dev` is required; synthesis fails without it. A single naming helper (`infra/naming.py`) derives every name:
   - main keeps today's names exactly (stacks unsuffixed, resources `oerf-…`, SSM `/oerf/…`);
   - dev uses `-dev` on stack names, `oerf-dev-` on resources, `/oerf/dev/` in SSM.
2. **Same account (906048429638) and region (us-east-1).** Account-level singletons are created only by main; dev imports what it needs by ARN.
3. **Separate CDK bootstrap for dev** (qualifier `oerfdev`, toolkit stack `CDKToolkit-dev`). Dev's GitHub deploy role (`OerfGitHubDeployRole-dev`, in `OerfCiStack-dev`) can only deploy `*-dev` stacks and trusts only the GitHub `dev` environment. `OerfCiStack-dev` and the bootstrap are deployed from a laptop, never from CI.
4. **Branches:** `main` is frozen at tag `mvp1-28-oct-2026`. Feature branches merge into `develop`; every merge runs CI and deploys dev. Feature branches never deploy.
5. **Operator tooling is stage-aware** (A7): scripts and `make` targets require `STAGE`, and main needs `ALLOW_MAIN=1`. `make dev-deploy` resolves values the same way as the dev workflow and refuses a deploy that would drop a value the dev stacks already have.
6. **No flag-dependent cross-stack exports** (A0 lesson): a resource that exists only under a flag keeps its alarm in its own stack, so toggling the flag cannot break an import.

## Consequences

- Dev can be rebuilt, rehearsed on and broken without touching main; main's templates are unchanged (checked by tests comparing synthesized templates).
- Both stages share account quotas (Lambda concurrency, Bedrock throughput, SES/SNS sandbox status, WhatsApp messaging limits).
- Within one account IAM can only narrow the boundary between stages; ADR-041 records how. The full boundary is a separate dev account (tracker E8).
- `main` and `develop` diverge until main is unfrozen; the first main deploy after the freeze needs a strict `cdk diff` (F8).

## Alternatives considered

- **Separate dev account:** the cleanest boundary, but blocked by the single WABA and its one event destination; tracked as E8.
- **Deploy branches to the same stacks:** overwrites main; rejected.
- **CDK stage prefixes without a naming helper:** would rename main's resources and replace stateful ones; rejected.
