locals {
  ecs_task_assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  job_secret_arn = "arn:${data.aws_partition.current.partition}:secretsmanager:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:secret:agent-wow/jobs/*"
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_region" "current" {}

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
        Effect = "Allow"
        Action = "s3:PutObject"
        Resource = [
          "${aws_s3_bucket.artifacts.arn}/runs/*",
          "${aws_s3_bucket.artifacts.arn}/viewer/*",
          "${aws_s3_bucket.artifacts.arn}/inputs/*"
        ]
      },
      {
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "${aws_s3_bucket.artifacts.arn}/clients/*"
      },
      {
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = aws_s3_bucket.artifacts.arn
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObjectVersion", "s3:DeleteObjectVersion"]
        Resource = "${aws_s3_bucket.artifacts.arn}/inputs/*"
      },
      {
        Effect    = "Allow"
        Action    = "ec2:RunInstances"
        Resource  = "*"
        Condition = { ArnEquals = { "ec2:LaunchTemplate" = aws_launch_template.client.arn } }
      },
      {
        Effect    = "Allow"
        Action    = "ec2:CreateTags"
        Resource  = "arn:${data.aws_partition.current.partition}:ec2:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:instance/*"
        Condition = { StringEquals = { "ec2:CreateAction" = "RunInstances" } }
      },
      {
        Effect    = "Allow"
        Action    = "ec2:TerminateInstances"
        Resource  = "arn:${data.aws_partition.current.partition}:ec2:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:instance/*"
        Condition = { StringEquals = { "ec2:ResourceTag/agent-wow-role" = "client" } }
      },
      { Effect = "Allow", Action = "ec2:DescribeInstances", Resource = "*" },
      {
        Effect    = "Allow"
        Action    = "iam:PassRole"
        Resource  = aws_iam_role.client.arn
        Condition = { StringEquals = { "iam:PassedToService" = "ec2.amazonaws.com" } }
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.jobs.arn
      },
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue",
          "secretsmanager:DeleteSecret"
        ]
        Resource = local.job_secret_arn
      }
    ]
  })
}

resource "aws_iam_role" "client" {
  name = "agent-wow-client"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_instance_profile" "client" {
  name = "agent-wow-client"
  role = aws_iam_role.client.name
}

resource "aws_iam_role_policy" "client" {
  name = "agent-wow-client-artifacts"
  role = aws_iam_role.client.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "s3:PutObject"
        Resource = "${aws_s3_bucket.artifacts.arn}/clients/$${aws:userid}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage"]
        Resource = aws_ecr_repository.runner.arn
      },
      { Effect = "Allow", Action = "ecr:GetAuthorizationToken", Resource = "*" }
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
      Effect    = "Allow"
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
