variable "codex_subscription_image_tag" {
  type    = string
  default = "build-codex-subscription-20261024-v1"
}

# Only the empty secret is managed by OpenTofu. Login data never enters state.
resource "aws_secretsmanager_secret" "codex_subscription" {
  name_prefix             = "agent-wow-codex-subscription-"
  description             = "Codex ChatGPT login cache; populated outside OpenTofu"
  recovery_window_in_days = 7
}

resource "aws_iam_role_policy" "codex_subscription_execution" {
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = aws_secretsmanager_secret.codex_subscription.arn
    }]
  })
}

resource "aws_batch_job_definition" "codex_subscription" {
  name                  = "agent-wow-codex-subscription"
  type                  = "container"
  platform_capabilities = ["EC2"]
  retry_strategy { attempts = 1 }
  timeout { attempt_duration_seconds = var.job_timeout_seconds }
  container_properties = jsonencode(merge(jsondecode(aws_batch_job_definition.viewer.container_properties), {
    image = "${aws_ecr_repository.agent.repository_url}:${var.codex_subscription_image_tag}"
    environment = [
      { name = "AWS_DEFAULT_REGION", value = var.aws_region },
      { name = "AWS_RESULTS_BUCKET", value = aws_s3_bucket.results.id },
      { name = "CODEX_AUTH_MODE", value = "subscription" },
      { name = "CODEX_MODEL_PROVIDER", value = "openai" },
      { name = "CODEX_MODEL", value = "gpt-6.1-sol" },
      { name = "CODEX_REASONING_EFFORT", value = "medium" }
    ]
    secrets = [{ name = "CODEX_AUTH_JSON", valueFrom = aws_secretsmanager_secret.codex_subscription.arn }]
  }))
  depends_on = [aws_iam_role_policy.codex_subscription_execution]
}

output "codex_subscription_job_definition" { value = aws_batch_job_definition.codex_subscription.arn }
output "codex_subscription_secret_arn" { value = aws_secretsmanager_secret.codex_subscription.arn }
