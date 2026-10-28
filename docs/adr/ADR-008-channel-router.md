# ADR-008: The channel router is the only sender

**Status:** Accepted
**Date:** 2026-10-03
**Decider:** Architect

## Decision

`oerf-channel-router` is the only component that sends to a person. It is the only role that calls AWS End User Messaging Social (`socialmessaging:SendWhatsAppMessage`), SMS, and Amazon SES (`ses:SendEmail`).

WhatsApp is the primary channel. A live send uses End User Messaging Social and an approved template (`oerf_<logical_name>`) with the body parameters in template order. A session acknowledgement after a valid reply is plain text. The origination phone number id comes from SSM. The state machines, EventBridge, and the step functions do not call these APIs.

The default channel order is WhatsApp, then SMS, then voice, then email, from tenant config. Inbound may call `GetWhatsAppMessageMedia` to store a photo. That call does not send.

## Reasons

Sends need one place for opt-in, template mapping, dedupe, and the outbound message id that a reply quotes. Splitting the SDKs across steps would make the audit and the IAM boundary harder to review.

## Consequences

`USE_MOCK_WHATSAPP`, `USE_MOCK_SMS`, and `USE_MOCK_EMAIL` default to true. They are false only when context `channels:live` is true. A mocked send still records the outbound id so a scripted reply can resume the workflow.

Live SMS uses `sns:Publish` on the router role. An early note named `pinpoint-sms-voice-v2`. That client is not in the router. SMS stays mocked until channels are live.

Live email uses SESv2 and `SES_SOURCE_EMAIL` on the router. A custom sending domain is not configured here. The report generator does not send email. An early IAM note granted that function `ses:SendEmail`. The deployed report role writes the report bucket only.

Voice does not place a call. A voice rung tells the person to reply on WhatsApp ([ADR-014](README.md)). RCS is not a deployed channel.

The WhatsApp event destination on the inbound topic is created only when a WhatsApp Business account id is supplied at deploy time.
