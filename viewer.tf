# Viewer code lives separately; infrastructure stays in this OpenTofu state.
module "viewer" {
  source = "git::https://github.com/TuckerBMorgan/agent-wow-viewer.git//terraform?ref=9ed637708f5f454ab65e5303e4a2d254c35e99d0"

  aws_region                    = var.aws_region
  partition                     = data.aws_partition.current.partition
  vpc_id                        = aws_vpc.agent.id
  availability_zone             = data.aws_availability_zones.available.names[1]
  route_table_id                = aws_route_table.agent.id
  security_group_id             = aws_security_group.agent.id
  instance_role_name            = aws_iam_role.agent["instance"].name
  instance_assume_role_policy   = aws_iam_role.agent["instance"].assume_role_policy
  instance_profile_arn          = aws_iam_instance_profile.agent.arn
  job_role_id                   = aws_iam_role.agent["job"].id
  results_bucket_arn            = aws_s3_bucket.results.arn
  ecr_repository_arn            = aws_ecr_repository.agent.arn
  ecr_repository_url            = aws_ecr_repository.agent.repository_url
  agent_launch_template_id      = aws_launch_template.agent.id
  agent_launch_template_version = aws_launch_template.agent.latest_version
}

output "viewer_launch_template" { value = module.viewer.viewer_launch_template }
output "viewer_subnet" { value = module.viewer.viewer_subnet }
output "viewer_subnets" { value = [module.viewer.viewer_subnet, aws_subnet.agent.id] }
output "world_build_template" { value = module.viewer.world_build_template }
output "world_watch_image" { value = module.viewer.world_watch_image }
output "viewer_job_queue" { value = module.viewer.viewer_job_queue }
