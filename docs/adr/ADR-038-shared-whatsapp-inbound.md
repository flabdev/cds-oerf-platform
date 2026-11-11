# ADR-038: One WhatsApp number shared by main and dev

- **Status:** Accepted
- **Date:** 2026-11-11
- **Tracker:** A5, F4, D1, D7, G4

## Context

The platform sends and receives WhatsApp through AWS End User Messaging Social. There is one WhatsApp Business Account (WABA) and one phone number. A WABA has exactly **one** event destination: every inbound message and delivery status for every number on it goes there. Main's stack owns that destination (an SNS inbound topic). The dev stage (ADR-037) needs live WhatsApp for rehearsals without a second number or a second WABA.

## Decision

1. **Outbound:** both stages send with the same phone number id. Templates belong to the WABA, so approved templates work for both. Each router's IAM is scoped to that phone number's ARN.
2. **Inbound:** the event destination stays on **main's** inbound topic. Dev subscribes its own inbound queue to that topic by ARN (`whatsapp:sharedInboundTopicArn`). Dev never passes `whatsapp:wabaId` and never creates or changes the destination; the dev workflow refuses it.
3. **Ownership:** each stage acts only on events it owns: a reply that quotes a message id the stage sent, or a sender in the stage's own routing table (`PHONE#` rows). Anything else is logged as `inbound.ignored reason=not_owned` without the number and dropped, with no write, retry or dead-letter.
4. **Sandbox `JOIN` and audience `JOIN DRIVER` are dev-only** (`SANDBOX_JOIN_ENABLED` is set only on dev; #67), so a stranger's message never opens a session on main.
5. **Templates with fixed desk links** (`oerf_ops_escalation_options`, `oerf_qa_report_ready`) point at main's desk. Dev gets stage-specific copies and the router chooses the name by stage (D1, D7).

## Consequences

- Rehearsals use real WhatsApp on dev at no extra provider cost.
- **Operating rule:** no incidents on main while dev rehearses. Free text is matched by sender; if the same phone had an open engagement in both stages, both would act.
- STOP / opt-out reaches both stages; each records it.
- Messaging limits, quality rating and the 24-hour service window are shared by both stages.
- Until the dev template copies are approved, the two desk-link templates sent from dev open main's desk; demos do escalation and QA on the dev desk.
- Moving the destination and topic into a shared stack owned by neither stage is tracked as D6 (after the Show & Tell).

## Alternatives considered

- **Second WABA or number for dev:** cleanest, but needs Meta onboarding and template approval again; not possible in the time frame.
- **Dev owns the destination during rehearsals:** would silence main and risks leaving it pointed at dev; rejected.
- **Forward from main's inbound handler to dev:** couples the stages in code; rejected in favour of SNS fan-out.
