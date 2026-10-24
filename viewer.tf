# Viewer code lives separately; infrastructure stays in this OpenTofu state.
module "viewer" {
  source = "git::https://github.com/TuckerBMorgan/agent-wow-viewer.git//terraform?ref=3e5f0adaa8f820ad46c944a0d27299b18710f404"

  aws_region                    = var.aws_region
  partition                     = data.aws_partition.current.partition
  vpc_id                        = aws_vpc.agent.id
  availability_zone             = data.aws_availability_zones.available.names[1]
  route_table_id                = aws_route_table.agent.id
  security_group_id             = aws_security_group.agent.id
  instance_role_name            = aws_iam_role.instance.name
  instance_assume_role_policy   = aws_iam_role.instance.assume_role_policy
  instance_profile_arn          = aws_iam_instance_profile.agent.arn
  job_role_id                   = aws_iam_role.job.id
  results_bucket_arn            = aws_s3_bucket.results.arn
  ecr_repository_arn            = aws_ecr_repository.agent.arn
  ecr_repository_url            = aws_ecr_repository.agent.repository_url
  agent_launch_template_id      = aws_launch_template.agent.id
  agent_launch_template_version = aws_launch_template.agent.latest_version
}

output "viewer_launch_template" { value = module.viewer.viewer_launch_template }
output "viewer_subnet" { value = module.viewer.viewer_subnet }
output "world_build_subnet" { value = module.viewer.world_build_subnet }
output "world_build_template" { value = module.viewer.world_build_template }
output "world_watch_image" { value = module.viewer.world_watch_image }
output "viewer_job_queue" { value = module.viewer.viewer_job_queue }

# Preserve deployed resources when their configuration moves into the module.
moved {
  from = aws_subnet.viewer
  to   = module.viewer.aws_subnet.viewer
}

moved {
  from = aws_route_table_association.viewer
  to   = module.viewer.aws_route_table_association.viewer
}

moved {
  from = aws_iam_role_policy_attachment.instance_ssm
  to   = module.viewer.aws_iam_role_policy_attachment.instance_ssm
}

moved {
  from = aws_iam_role_policy.viewer_game
  to   = module.viewer.aws_iam_role_policy.viewer_game
}

moved {
  from = aws_iam_role.viewer
  to   = module.viewer.aws_iam_role.viewer
}

moved {
  from = aws_iam_role_policy_attachment.viewer_ssm
  to   = module.viewer.aws_iam_role_policy_attachment.viewer_ssm
}

moved {
  from = aws_iam_role_policy.viewer
  to   = module.viewer.aws_iam_role_policy.viewer
}

moved {
  from = aws_iam_instance_profile.viewer
  to   = module.viewer.aws_iam_instance_profile.viewer
}

moved {
  from = aws_ssm_document.viewer_ports
  to   = module.viewer.aws_ssm_document.viewer_ports
}

moved {
  from = aws_launch_template.viewer
  to   = module.viewer.aws_launch_template.viewer
}

moved {
  from = aws_iam_role.world_build
  to   = module.viewer.aws_iam_role.world_build
}

moved {
  from = aws_iam_role_policy_attachment.world_build_ssm
  to   = module.viewer.aws_iam_role_policy_attachment.world_build_ssm
}

moved {
  from = aws_iam_role_policy.world_build
  to   = module.viewer.aws_iam_role_policy.world_build
}

moved {
  from = aws_iam_instance_profile.world_build
  to   = module.viewer.aws_iam_instance_profile.world_build
}

moved {
  from = aws_launch_template.world_build
  to   = module.viewer.aws_launch_template.world_build
}

moved {
  from = aws_batch_compute_environment.viewer
  to   = module.viewer.aws_batch_compute_environment.viewer
}

moved {
  from = aws_batch_job_queue.viewer
  to   = module.viewer.aws_batch_job_queue.viewer
}
