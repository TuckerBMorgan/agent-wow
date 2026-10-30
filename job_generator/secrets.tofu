locals {
  runner_harnesses = toset(["claude", "codex"])
}

resource "aws_secretsmanager_secret" "runner_credentials" {
  for_each = local.runner_harnesses

  name                    = "agent-wow/runner/${each.key}"
  recovery_window_in_days = 30
}

output "runner_credential_secret_arns" {
  value = {
    for harness, secret in aws_secretsmanager_secret.runner_credentials :
    harness => secret.arn
  }
}
