# Managed login branding

Dev turns this on with CDK context `auth:managedLogin=true` (the dev deploy default). That sets the dashboard domain's managed login version to 2 and creates one `AWS::Cognito::ManagedLoginBranding` for the dashboard app client (`UserPoolClientId`). The user pool is already on the Essentials feature plan. Main does not pass the flag.

There is no `managed-login-branding.json` until you export one. Without that file, the branding resource uses Cognito's default style (`UseCognitoProvidedValues: true`). CloudFormation rejects `Settings` and `Assets` on that request, so the default style cannot include the OERF logo. Add the logo in the branding editor, then export.

## Export after editing in the console

Console edits apply immediately and are overwritten by the next deploy unless you export them first.

1. Edit the style in the Cognito console branding editor on the dev user pool. Assign it to the dashboard app client. `UserPoolId` and `UserPoolClientId` are outputs of `OerfApiStack-dev`. Do not paste those ids into a ticket or this file's instructions.
2. Export the merged style. Replace the two ids with the stack outputs:

```bash
aws cognito-idp describe-managed-login-branding-by-client \
  --region us-east-1 \
  --user-pool-id "$USER_POOL_ID" \
  --client-id "$CLIENT_ID" \
  --return-merged-resources
```

3. Save `ManagedLoginBranding.Settings` and `ManagedLoginBranding.Assets` as `infra/config/managed-login-branding.json`:

```json
{
  "Settings": {},
  "Assets": []
}
```

Drop the `ManagedLoginBranding` wrapper. Keep each asset's `Category`, `ColorMode`, `Extension`, and `Bytes` (base64). `ResourceId` may be included. Do not commit the user pool id, client id, or the API wrapper.

4. Commit the file and redeploy dev with `-c auth:managedLogin=true`. The template then sets `UseCognitoProvidedValues` to false and applies `Settings` and `Assets`.

## Asset limits

From the CloudFormation `AssetType` property and the branding designer image table:

- At most 40 assets. The whole create or update request must be 2 MB or smaller. `Bytes` is base64 and at most 1,000,000 characters.
- `ColorMode`: `LIGHT`, `DARK`, or `DYNAMIC`.
- `Extension`: `ICO`, `JPEG`, `PNG`, `SVG`, or `WEBP`.
- `FORM_LOGO` (the form logo) accepts PNG, SVG, and JPEG. The branding designer table does not list WEBP or ICO for that category.

The desk logo is `web/public/logo.png` (PNG, 640×640, 18511 bytes). The same file is `docs/images/oerf-logo.png`. As a `FORM_LOGO` asset its base64 form is under the `Bytes` limit. It is not embedded while the style is still the Cognito default.
