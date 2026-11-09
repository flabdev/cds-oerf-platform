# Photo evidence evaluation

`labels.json` describes 18 evaluation cases, but this repository does **not**
include the corresponding photographs. The labels are not evidence that a real
photo corpus exists. For a meaningful model evaluation, obtain permissioned,
de-identified reefer-display photos from the device/test-data owner and verify
every expected value against its photo. Do not use customer-identifiable images
without approval, or pair unrelated photos with these labels.

## Real-photo evaluation

Put each verified image beside the labels file, using the filename in its
`file` field (for example, `clear_01.png`). Alternatively, keep the dataset in
an approved private directory: the runner looks for images beside whichever
labels file is supplied with `--labels`.

With AWS credentials and access to the configured Bedrock model, run:

```bash
python -m scripts.eval_photo_evidence --labels /approved/dataset/labels.json --json
```

Do **not** pass `--allow-generated-fixtures` for a quality evaluation. Ensure
the labels accurately match the photos; update or replace `labels.json` as
needed before interpreting scores. Keep sensitive photographs in the approved
private dataset location rather than committing them to the repository.

## Synthetic development smoke test only

The renderer creates flat-color placeholders, not reefer photos. It writes PNGs
beside this directory's `labels.json` and overwrites files with matching names,
so use it only when no real images are present. From the repository root:

```bash
python tests/ai-evals/photos/render.py
python -m scripts.eval_photo_evidence --allow-generated-fixtures
```

The flag permits a development smoke run; the report must say
`quality_evidence: false`. The generated PNGs are local artifacts and should not
be committed. Remove them after the smoke test. Unit tests use temporary PNGs
and a fake Bedrock client, require no credentials, and are not model-quality
evaluations:

```bash
pytest -q tests/unit/evals/test_photo_evidence.py
```
