output "source_bucket" { value = aws_s3_bucket.this["source"].id }
output "source_bucket_arn" { value = aws_s3_bucket.this["source"].arn }
output "artifacts_bucket" { value = aws_s3_bucket.this["artifacts"].id }
output "artifacts_bucket_arn" { value = aws_s3_bucket.this["artifacts"].arn }
output "config_bucket" { value = aws_s3_bucket.this["config"].id }
output "config_bucket_arn" { value = aws_s3_bucket.this["config"].arn }
