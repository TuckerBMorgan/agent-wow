resource "aws_ecr_repository" "runner" {
  name                 = "agent-wow-runner"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false

  encryption_configuration {
    encryption_type = "AES256"
  }

  image_scanning_configuration {
    scan_on_push = true
  }
}

output "runner_repository_url" {
  value = aws_ecr_repository.runner.repository_url
}
