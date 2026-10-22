# Provider onboarding and channel readiness

This runbook is the OERF-311 checklist. It names the secret ids and the checks
a demo can run without credentials. It does not create Secrets Manager
resources or IAM. That is OERF-301 (Ananda) and OERF-310. It does not mark any
channel production-ready. The AWS account owner finishes the external
approvals and fills the secret values outside this repository.

No token, password, access key, or live phone number belongs in git, logs, or
fixtures. Seed phones such as `${PHONE_T1_DRIVER}` and `qa@example.com` are
placeholders.

The probe is `channel_readiness` in `oerf/platform/channels/readiness.py`. It
reads environment flags and returns one of `simulated`, `not_configured`, or
`external_approval`. It does not call a provider.

## Status

| Channel | Secret name | Status today | External owner | What is still open |
| --- | --- | --- | --- | --- |
| WhatsApp | `oerf/whatsapp/phone_number_id` | external approval | AWS account owner | Business registration, approved templates, webhook |
| SMS | `oerf/pinpoint/origination_number` | external approval | AWS account owner | Pinpoint origination number |
| Email, outbound | `oerf/ses/source_identity` | external approval | AWS account owner | SPF, DKIM, production sending access |
| Email, inbound QA | `oerf/ses/inbound_rule` and `oerf/ses/qa_allowlist` | external approval | AWS account owner | Receipt rule, spam and virus checks |
| Voice | `oerf/pinpoint/voice_origination` | not configured | AWS account owner | No voice SDK is wired |
| RCS | `oerf/rcs/agent` | external approval | AWS account owner | Inbound reply test |

Rollback for a demo is `USE_MOCK_WHATSAPP`, `USE_MOCK_SMS`, or `USE_MOCK_EMAIL`
set to `true`, or sandbox `dashboard_only`. Those modes do not delete secrets.
Secret rotation is a new secret version created by the account owner. The
application does not write secret values.

## WhatsApp and RCS

Sender identity is the End User Messaging Social phone number id, supplied at
runtime as `WHATSAPP_PHONE_NUMBER_ID` from `oerf/whatsapp/phone_number_id`.
The message body is rendered by the channel router from the template catalog
in `domains/cold_chain_excursion/messages.json`. Only `driver_checkin` has SMS
text today. Other template ids fail that channel with `no_template` and the
router tries the next channel.

Approval still open: WhatsApp Business registration, template approval, and
the inbound webhook. Rate limits are whatever the approved account allows.
They are not recorded here because they are not known yet.

The inbound webhook is the compute stack's standard SNS topic. It is not
encrypted with a customer-managed KMS key, so no key policy is granted to
End User Messaging Social. Deploy with context `whatsapp:wabaId` set to the
`waba-...` id to allow that service to publish to the topic and to set the
WABA event destination. Without that context value the stack does not add
the topic statement or the destination. The id is not stored in git.

RCS is not a default channel. The router tries it only when the request has
`inbound_rcs` and the role is in `rcs_roles`. Otherwise the attempt reason is
`rcs_unavailable` and the RCS provider is not called. An inbound RCS reply
test is still an account-owner step. Secret name: `oerf/rcs/agent`.

## SMS and voice

SMS origination is `oerf/pinpoint/origination_number`. Voice origination is
`oerf/pinpoint/voice_origination`. Neither value is in this repository.
`VoiceProvider.send` returns `voice_provider_not_configured` and `delivered`
false. The current SMS function can call SNS when mock mode is off. That call
is not evidence of a Pinpoint origination number or of production access.

## Email

Outbound identity is `oerf/ses/source_identity`, read at runtime as
`SES_SOURCE_EMAIL`. When that value is empty the send returns failure
`SES_SOURCE_EMAIL not configured` and does not call SES.

Account-owner steps, all still open:

- Verify the sending domain with SPF and DKIM.
- Request SES production access. Until that is granted, the channel stays
  `external_approval` even if a source address is present.
- Add an inbound receipt rule for the QA reply address. Turn on spam and
  virus scanning. Point the rule at the inbound normalizer (OERF-205). The
  rule id is a reference in `oerf/ses/inbound_rule`, not a rule created here.

## QA reply allowlist

A QA reply is accepted only when `qa_reply_allowed` is true. The sender must
be exactly an email on the tenant's opted-in contacts whose role is `qa` or
`qa_lead`. Another role, another tenant's address, a display name, and an
empty list are rejected. Rejection happens before correlation.

`oerf/ses/qa_allowlist` is the secret OERF-301 should create for the same
addresses. The receipt rule should accept only those addresses. The
application check still runs. Passing the check does not mean SES delivered
anything.

OERF-117 publishes the HTML report and does not send it. A later send of that
report goes through the channel router.

## Secret access

OERF-301 creates the secrets named above and grants the channel-router role
`secretsmanager:GetSecretValue` on those names only. The role does not get
`PutSecretValue`. Runtime values are written by the account owner. This
repository does not contain them.

## Simulated and delivered

`delivered: true` means a provider accepted the message. A mock or a fake
simulated send is `delivered: false`, `simulated: true`, and reason
`simulated`. The router stops there. It does not continue to a later live
channel, and it does not record the attempt as a delivery.

`FakeProvider(..., simulated=True)` is the credential-free path for a demo
and for tests. `USE_MOCK_WHATSAPP`, `USE_MOCK_SMS`, and `USE_MOCK_EMAIL`
make the legacy senders return status `SIMULATED` with `success: false`.
The adapter maps that to the same simulated result.

Channel order when a send is a real failure, not a simulation, stays
WhatsApp, SMS, voice, email. Opt-out is checked before any attempt. A
missing template skips that channel. A missing fact raises before any send.

## Probe

```python
from oerf.platform.channels.readiness import channel_readiness

for row in channel_readiness():
    print(row.channel, row.status, row.secret_name)
```

A row is never `production_ready`. `external_approval` means this repository
cannot see the account owner's approval, so the channel is not live.
