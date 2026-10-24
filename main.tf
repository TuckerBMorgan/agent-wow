# AWS Batch starts EC2 capacity for queued runs and scales back to zero when idle.
# Configure AWS credentials first (for example, aws sso login), then:
#   tofu init
#   tofu plan
#   tofu apply
# Apply creates infrastructure only. Before submitting a job:
#   1. Build this Dockerfile for linux/amd64 and push it to image_uri below.
#   2. Populate the account login secrets outside OpenTofu so they never enter its state.
#   3. Run the submit_job command printed by tofu output.
# Use a new image_tag for each new image build; apply then registers that version.
# Commit and push application changes to your fork before build_and_push: the
# Dockerfile fetches application source at the local Git commit from that fork.
# Every submitted job gets fresh game storage. Reports survive in S3; game disks do not.
# Compute scales down after an AWS-managed delay. Images, secrets, logs and results
# remain billable. Nonempty ECR/S3 resources block destroy until their contents
# are explicitly removed; OpenTofu does not force-delete saved images or results.

terraform {
  required_version = ">= 1.8.0, < 2.0.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 6.69.1"
    }
  }
}

variable "aws_region" {
  description = "AWS region for the job infrastructure."
  type        = string
  default     = "us-west-2"
}

variable "image_tag" {
  description = "Unique tag of the linux/amd64 image you will push to ECR."
  type        = string
  default     = "initial"
  validation {
    condition     = can(regex("^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$", var.image_tag)) && var.image_tag != "latest"
    error_message = "Use a valid, unique image tag instead of latest (for example, build-001)."
  }
}

variable "job_timeout_seconds" {
  description = "Maximum running time per job, including game setup and agent work."
  type        = number
  default     = 21600
  validation {
    condition     = var.job_timeout_seconds >= 60 && floor(var.job_timeout_seconds) == var.job_timeout_seconds
    error_message = "The timeout must be a whole number of at least 60 seconds."
  }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = { Project = "agent-wow" }
  }
}

data "aws_partition" "current" {}
data "aws_availability_zones" "available" {
  state = "available"
}

# Public outbound internet avoids a permanently billable NAT gateway. No inbound
# traffic is allowed, including SSH and game ports; watch the job in CloudWatch.
resource "aws_vpc" "agent" {
  cidr_block           = "10.42.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
}

resource "aws_subnet" "agent" {
  vpc_id                  = aws_vpc.agent.id
  cidr_block              = "10.42.0.0/24"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = true
}

resource "aws_internet_gateway" "agent" {
  vpc_id = aws_vpc.agent.id
}

resource "aws_route_table" "agent" {
  vpc_id = aws_vpc.agent.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.agent.id
  }
}

resource "aws_route_table_association" "agent" {
  subnet_id      = aws_subnet.agent.id
  route_table_id = aws_route_table.agent.id
}

resource "aws_security_group" "agent" {
  name_prefix = "agent-wow-"
  description = "Outbound only for game image downloads and the agent API"
  vpc_id      = aws_vpc.agent.id
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_ecr_repository" "agent" {
  name                 = "agent-wow"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false
  image_scanning_configuration {
    scan_on_push = true
  }
}

# Only empty secrets are managed here. Login data never enters OpenTofu state.
resource "aws_secretsmanager_secret" "codex_subscription" {
  name_prefix             = "agent-wow-codex-subscription-"
  description             = "Codex ChatGPT login cache; populated outside OpenTofu"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret" "claude_subscription" {
  name_prefix             = "agent-wow-claude-subscription-"
  description             = "Claude Code subscription OAuth token; populated outside OpenTofu"
  recovery_window_in_days = 7
}

resource "aws_cloudwatch_log_group" "agent" {
  name              = "/aws/batch/agent-wow"
  retention_in_days = 14
}

resource "aws_s3_bucket" "results" {
  bucket_prefix = "agent-wow-results-"
  force_destroy = false
}

resource "aws_s3_bucket_public_access_block" "results" {
  bucket                  = aws_s3_bucket.results.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "results" {
  bucket = aws_s3_bucket.results.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_policy" "results" {
  bucket = aws_s3_bucket.results.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.results.arn, "${aws_s3_bucket.results.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

# EC2 hosts ECS; the job itself gets only results-upload access.
resource "aws_iam_role" "instance" {
  name_prefix = "agent-wow-instance-"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow", Action = "sts:AssumeRole"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "instance" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role"
}

resource "aws_iam_instance_profile" "agent" {
  name_prefix = "agent-wow-"
  role        = aws_iam_role.instance.name
}

locals {
  task_trust = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow", Action = "sts:AssumeRole"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })
  image_uri = "${aws_ecr_repository.agent.repository_url}:${var.image_tag}"
}

resource "aws_iam_role" "execution" {
  name_prefix        = "agent-wow-execution-"
  assume_role_policy = local.task_trust
}

resource "aws_iam_role_policy" "execution" {
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage"]
        Resource = aws_ecr_repository.agent.arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.agent.arn}:*"
      },
      {
        Effect = "Allow", Action = ["secretsmanager:GetSecretValue"]
        Resource = [aws_secretsmanager_secret.codex_subscription.arn,
        aws_secretsmanager_secret.claude_subscription.arn]
      }
    ]
  })
}

resource "aws_iam_role" "job" {
  name_prefix        = "agent-wow-job-"
  assume_role_policy = local.task_trust
}

resource "aws_iam_role_policy" "job" {
  role = aws_iam_role.job.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "${aws_s3_bucket.results.arn}/runs/*"
      }
    ]
  })
}

resource "aws_launch_template" "agent" {
  name_prefix = "agent-wow-"
  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = 100
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }
  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }
}

resource "aws_batch_compute_environment" "agent" {
  name_prefix = "agent-wow-"
  type        = "MANAGED"
  state       = "ENABLED"
  # Omission lets AWS create/reuse AWSServiceRoleForBatch, which also supports
  # later infrastructure updates. First apply needs iam:CreateServiceLinkedRole.
  compute_resources {
    type                = "EC2"
    allocation_strategy = "BEST_FIT_PROGRESSIVE"
    min_vcpus           = 0
    desired_vcpus       = 0
    max_vcpus           = 32
    instance_type       = ["m6i.2xlarge"]
    instance_role       = aws_iam_instance_profile.agent.arn
    subnets             = [aws_subnet.agent.id]
    security_group_ids  = [aws_security_group.agent.id]
    ec2_configuration {
      image_type = "ECS_AL2023"
    }
    launch_template {
      launch_template_id = aws_launch_template.agent.id
      version            = tostring(aws_launch_template.agent.latest_version)
    }
  }
  # AWS owns the changing desired capacity. Never reset it during another apply.
  lifecycle {
    ignore_changes = [compute_resources[0].desired_vcpus]
  }
  depends_on = [
    aws_iam_role_policy_attachment.instance,
    aws_route_table_association.agent
  ]
}

resource "aws_batch_job_queue" "agent" {
  name     = "agent-wow"
  state    = "ENABLED"
  priority = 1
  compute_environment_order {
    order               = 1
    compute_environment = aws_batch_compute_environment.agent.arn
  }
}

# Tests take available capacity ahead of the normal queue; running jobs continue.
resource "aws_batch_job_queue" "test" {
  name     = "agent-wow-test"
  state    = "ENABLED"
  priority = 100
  compute_environment_order {
    order               = 1
    compute_environment = aws_batch_compute_environment.agent.arn
  }
}

# Both harnesses use the same game image, resources, logging, and result storage.
# ECS supplies only the selected account login; the gameplay role cannot fetch it.
locals {
  job_environment = [
    { name = "AWS_DEFAULT_REGION", value = var.aws_region },
    { name = "AWS_RESULTS_BUCKET", value = aws_s3_bucket.results.id }
  ]
  job_container = {
    image            = local.image_uri
    command          = ["run-wow-agent"]
    privileged       = true
    jobRoleArn       = aws_iam_role.job.arn
    executionRoleArn = aws_iam_role.execution.arn
    resourceRequirements = [
      { type = "VCPU", value = "8" },
      { type = "MEMORY", value = "28672" }
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.agent.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "agent-wow"
      }
    }
  }
}

resource "aws_batch_job_definition" "codex_subscription" {
  name                  = "agent-wow-codex-subscription"
  type                  = "container"
  platform_capabilities = ["EC2"]
  retry_strategy { attempts = 1 }
  timeout { attempt_duration_seconds = var.job_timeout_seconds }
  container_properties = jsonencode(merge(local.job_container, {
    environment = concat(local.job_environment, [
      { name = "AGENT_WOW_HARNESS", value = "codex" }
    ])
    secrets = [{ name = "CODEX_AUTH_JSON", valueFrom = aws_secretsmanager_secret.codex_subscription.arn }]
  }))
  depends_on = [aws_iam_role_policy.execution, aws_iam_role_policy.job]
}

resource "aws_batch_job_definition" "claude_subscription" {
  name                  = "agent-wow-claude-subscription"
  type                  = "container"
  platform_capabilities = ["EC2"]
  retry_strategy { attempts = 1 }
  timeout { attempt_duration_seconds = var.job_timeout_seconds }
  container_properties = jsonencode(merge(local.job_container, {
    environment = concat(local.job_environment, [
      { name = "AGENT_WOW_HARNESS", value = "claude" }
    ])
    secrets = [{ name = "CLAUDE_CODE_OAUTH_TOKEN", valueFrom = aws_secretsmanager_secret.claude_subscription.arn }]
  }))
  depends_on = [aws_iam_role_policy.execution, aws_iam_role_policy.job]
}

output "region" {
  value = var.aws_region
}
output "ecr_repository_url" {
  value = aws_ecr_repository.agent.repository_url
}
output "image_uri" {
  description = "Push a linux/amd64 build here before submitting the job."
  value       = local.image_uri
}
output "codex_subscription_secret_arn" {
  value = aws_secretsmanager_secret.codex_subscription.arn
}
output "job_queue" {
  value = aws_batch_job_queue.agent.name
}
output "test_job_queue" {
  value = aws_batch_job_queue.test.name
}
output "job_definitions" {
  value = {
    codex  = aws_batch_job_definition.codex_subscription.arn
    claude = aws_batch_job_definition.claude_subscription.arn
  }
}
output "claude_subscription_secret_arn" {
  description = "Populate with the subscription token outside OpenTofu."
  value       = aws_secretsmanager_secret.claude_subscription.arn
}
output "results_bucket" {
  value = aws_s3_bucket.results.id
}
output "log_group" {
  value = aws_cloudwatch_log_group.agent.name
}
output "submit_job" {
  description = "Each invocation starts a fresh run; apply alone never submits a job."
  value       = "aws batch submit-job --region ${var.aws_region} --job-name agent-wow --job-queue ${aws_batch_job_queue.agent.name} --job-definition ${aws_batch_job_definition.codex_subscription.arn}"
}

output "build_and_push" {
  description = "Run from this repository after pushing the application commit to your GitHub fork."
  value       = <<-COMMAND
    aws ecr get-login-password --region ${var.aws_region} | docker login --username AWS --password-stdin ${split("/", aws_ecr_repository.agent.repository_url)[0]}
    docker buildx build --platform linux/amd64 --build-arg AGENT_WOW_REF="$(git rev-parse HEAD)" --tag ${local.image_uri} --push .
  COMMAND
}
output "watch_logs" {
  value = "aws logs tail ${aws_cloudwatch_log_group.agent.name} --region ${var.aws_region} --follow"
}
output "results_location" {
  value = "s3://${aws_s3_bucket.results.id}/runs/<job-id>/"
}
