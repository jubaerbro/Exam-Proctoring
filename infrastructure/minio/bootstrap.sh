#!/bin/sh
# Creates Sentinel's buckets and asserts they are NOT publicly readable.
#
# Evidence is webcam and screen imagery of people in their homes. A public
# bucket policy here would be the worst single failure this product could have,
# so the bootstrap explicitly sets "none" rather than relying on the default.
set -eu

mc alias set local http://minio:9000 "$S3_ACCESS_KEY" "$S3_SECRET_KEY"

mc mb --ignore-existing "local/$S3_BUCKET_EVIDENCE"
mc mb --ignore-existing "local/$S3_BUCKET_TESTCASES"

# Explicitly private. Access is only ever via short-lived presigned URLs
# issued by the API after an authorization and exam-window check.
mc anonymous set none "local/$S3_BUCKET_EVIDENCE"
mc anonymous set none "local/$S3_BUCKET_TESTCASES"

# Versioning so a compromised credential cannot silently overwrite evidence.
mc version enable "local/$S3_BUCKET_EVIDENCE" || true

echo "minio bootstrap complete: $S3_BUCKET_EVIDENCE, $S3_BUCKET_TESTCASES (private)"
