## Summary

## Checklist

- [ ] Tests: `ruff check .` and `pytest -q -m "not aws"`. `node --test` in `web/` when the dashboard changes. `cdk synth` when infrastructure changes.
- [ ] Tenant safety: `tenant_id` comes from the Cognito token or the device registry. Keys, S3 prefixes, and logs carry it.
- [ ] Idempotency: a repeated request does not apply the same effect twice.
- [ ] Interface change: `docs/interfaces.md` matches the contract, or this pull request leaves the contract as it is.
- [ ] IAM review: permissions are least privilege. Any `*` is listed in `docs/security/iam-exceptions.md`.
- [ ] Logs have no secrets or PII: no tokens, task tokens, phone numbers, names, prompts, message bodies, or raw model output.
