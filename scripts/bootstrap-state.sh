#!/usr/bin/env bash
# Creates the Terraform state bucket (versioned, KMS-encrypted, public access blocked) for one environment.
# CREATES BILLABLE RESOURCES — run only with explicit approval.  Usage: scripts/bootstrap-state.sh dev
set -euo pipefail
ENV="${1:?env}"; REGION="${AWS_REGION:-us-east-2}"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKET="erp-tfstate-${ACCOUNT}-${ENV}"
echo "About to create s3://${BUCKET} in ${REGION} (account ${ACCOUNT}). Ctrl-C to abort."; read -r _
aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" --create-bucket-configuration LocationConstraint="$REGION"
aws s3api put-bucket-versioning --bucket "$BUCKET" --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket "$BUCKET" --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"aws:kms"},"BucketKeyEnabled":true}]}'
aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
sed "s/<account-id>/${ACCOUNT}/" "infra/terraform/envs/${ENV}/backend.hcl.example" > "infra/terraform/envs/${ENV}/backend.hcl"
echo "Wrote infra/terraform/envs/${ENV}/backend.hcl"
