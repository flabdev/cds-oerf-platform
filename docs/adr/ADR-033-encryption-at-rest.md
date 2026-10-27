# ADR-033: Encryption at rest uses AWS managed keys

**Status:** Accepted
**Date:** 2026-10-22
**Decider:** Architect

## Decision

The four DynamoDB tables use the AWS managed key `aws/dynamodb`. The evidence and report buckets use SSE-KMS with the AWS managed key `aws/s3` and S3 Bucket Keys. The dashboard bucket stays SSE-S3. SQS stays SQS-managed SSE. The inbound topic stays without a customer-managed key. There is no customer-managed key in the MVP.

**Note (2026-10-27):** Table encryption is deferred behind context `dynamodb:kmsManaged` (default `false`). A DynamoDB table's encryption mode can change only 4 times per 24 hours, then once per 6 hours. Failed deploys and rollbacks used that quota. With the flag off, the four tables omit `encryption` and stay on the AWS owned key. Enable `dynamodb:kmsManaged=true` after the demo, in a single deploy, once the window has reset. The bucket SSE-KMS change is unchanged.

## Reasons

A customer-managed key adds cost and a key policy. End User Messaging Social and CloudFront would both need a grant on that key. The dashboard bucket cannot use `aws/s3`: CloudFront origin access control cannot be granted `kms:Decrypt` on the AWS managed key, and the objects are public static assets. That key policy cannot be edited.

## Consequences

CloudFormation updates `SSESpecification` on `AWS::DynamoDB::Table` and `BucketEncryption` on `AWS::S3::Bucket` with no interruption. Neither change replaces the table or the bucket. Objects already stored under SSE-S3 are not rewritten. New writes use `aws/s3`.

An in-account role that calls DynamoDB or S3 does not need `kms:Decrypt` for `aws/dynamodb` or `aws/s3`. Those services use the keys. This change adds no `kms:` grant.

`GetWhatsAppMessageMedia` with `destinationS3File` retrieves the file from Meta and stores it in the named bucket. The request is a bucket name and a key. There is no encryption field. The service writes the object. The other destination, `destinationS3PresignedUrl`, is still an upload performed by the service. See [GetWhatsAppMessageMedia](https://docs.aws.amazon.com/social-messaging/latest/APIReference/API_GetWhatsAppMessageMedia.html), [S3File](https://docs.aws.amazon.com/social-messaging/latest/APIReference/API_S3File.html), and [Download a media file from WhatsApp to Amazon S3](https://docs.aws.amazon.com/social-messaging/latest/userguide/receive-message-image.html).

A service that writes the object on our behalf cannot use `aws/s3` the way an in-account role can. The AWS managed key policy cannot be edited to name that service. This call is not implemented. If inbound media must land in S3, the fallback is one of these, and neither is built now:

- The Lambda downloads the media bytes and `PutObject` writes them, so the caller is an in-account role.
- A separate SSE-S3 prefix or bucket holds media that a service writes itself.

A customer-managed key remains the post-MVP option when a service principal has to be named on the key.
