# ADR-006: Evidence processing

**Status:** Accepted
**Date:** 2026-10-03
**Decider:** Architect

## Decision

The evidence analyzer reads a live photo with Amazon Bedrock. The model is Amazon Nova Lite ([ADR-020](ADR-020-bedrock-model.md)). A guardrail is applied on the live Converse call. Low confidence stays a case for a person. The result is checked against the tenant fault-code catalog.

Document extraction and audio transcription are not part of this release. Textract is not used.

## Reasons

The demo evidence is a photo of a reefer display. A multimodal model covers that without a separate OCR pipeline. An asynchronous document workflow is off the critical path.

## Consequences

`oerf-evidence-analyzer` is deployed. `USE_MOCK_BEDROCK` stays true unless context `channels:live` is true.

Inbound stores WhatsApp media with End User Messaging `GetWhatsAppMessageMedia` and a presigned PUT into the evidence bucket. The handler does not download the bytes and does not read a Meta token. An earlier contract note described a direct download from Meta. The End User Messaging call is what the handler does.

The vision role may invoke Nova Lite and apply the guardrail. It does not send messages.
