#!/usr/bin/env bash
# Requires AGE_RECIPIENT and BACKUP_S3_PREFIX in the calling environment.
# Public age recipient belongs on VPS; keep decryption identity off the VPS.
set -Eeuo pipefail
umask 077
cd "$(dirname "$0")/.."
: "${AGE_RECIPIENT:?Set the off-host age public recipient}"
: "${BACKUP_S3_PREFIX:?Set an owned S3 backup prefix}"
[[ "$BACKUP_S3_PREFIX" == s3://* ]] || exit 2
command -v age >/dev/null
command -v aws >/dev/null
source deploy/versions.env
mkdir -p backups
stamp=$(date -u +%Y%m%dT%H%M%SZ)
output="backups/$stamp.dump.age"
trap 'rm -f "$output.partial"' EXIT
docker run --rm --network hivemind_backend --env-file deploy/admin.env "$POSTGRES_IMAGE" \
  pg_dump --format=custom --compress=6 --no-password |
  age --recipient "$AGE_RECIPIENT" > "$output.partial"
mv "$output.partial" "$output"
sha256sum "$output" > "$output.sha256"
aws s3 cp "$output" "${BACKUP_S3_PREFIX%/}/$stamp.dump.age" --sse AES256 --only-show-errors
aws s3 cp "$output.sha256" "${BACKUP_S3_PREFIX%/}/$stamp.dump.age.sha256" --sse AES256 --only-show-errors
printf 'Encrypted backup uploaded: %s/%s.dump.age\n' "${BACKUP_S3_PREFIX%/}" "$stamp"
