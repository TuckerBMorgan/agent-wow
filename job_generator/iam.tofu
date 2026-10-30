locals {
  ecs_task_assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role" "runner_job" {
  name               = "agent-wow-runner-job"
  assume_role_policy = local.ecs_task_assume_role_policy
}

resource "aws_iam_role_policy" "runner_job" {
  name = "agent-wow-runner-access"
  role = aws_iam_role.runner_job.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "${aws_s3_bucket.artifacts.arn}/params/*"
      },
      {
        Effect = "Allow"
        Action = "s3:PutObject"
        Resource = [
          "${aws_s3_bucket.artifacts.arn}/runs/*",
          "${aws_s3_bucket.artifacts.arn}/viewer/*"
        ]
      },
      {
        Effect   = "Allow"
        Action   = "dynamodb:UpdateItem"
        Resource = aws_dynamodb_table.jobs.arn
      },
      {
        Effect = "Allow"
        Action = "secretsmanager:GetSecretValue"
        Resource = [
          for secret in aws_secretsmanager_secret.runner_credentials :
          secret.arn
        ]
      }
    ]
  })
}

resource "aws_iam_role" "runner_execution" {
  name               = "agent-wow-runner-execution"
  assume_role_policy = local.ecs_task_assume_role_policy
}

resource "aws_iam_role_policy_attachment" "runner_execution" {
  role       = aws_iam_role.runner_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role" "batch_instance" {
  name = "agent-wow-batch-instance"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "batch_instance" {
  role       = aws_iam_role.batch_instance.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role"
}

resource "aws_iam_instance_profile" "batch_instance" {
  name = "agent-wow-batch-instance"
  role = aws_iam_role.batch_instance.name
}
