resource "aws_s3_object" "config_file" {
  bucket = aws_s3_bucket.app.id
  key    = "config/config.yaml"
  source = "${path.module}/../../services/trusted-actions/config/config.yaml"
  etag   = filemd5("${path.module}/../../services/trusted-actions/config/config.yaml")
}
