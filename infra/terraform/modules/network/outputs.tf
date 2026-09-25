output "vpc_id" {
  description = "VPC id."
  value       = aws_vpc.this.id
}

output "vpc_cidr" {
  description = "VPC CIDR block."
  value       = aws_vpc.this.cidr_block
}

output "availability_zones" {
  description = "AZs the subnets occupy."
  value       = local.azs
}

output "public_subnet_ids" {
  description = "Public subnets. The ALB lives here."
  value       = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  description = "Private subnets. RDS and, unless nat_strategy is none_public, the tasks."
  value       = aws_subnet.private[*].id
}

output "task_subnet_ids" {
  description = <<-EOT
    Subnets ECS tasks run in. Private unless nat_strategy is none_public, in
    which case tasks need a public IP to reach the internet at all.
  EOT
  value       = local.public_tasks ? aws_subnet.public[*].id : aws_subnet.private[*].id
}

output "assign_public_ip" {
  description = "Whether ECS tasks must be given a public IP (true only for nat_strategy = none_public)."
  value       = local.public_tasks
}

output "database_subnet_ids" {
  description = "Subnets for the RDS subnet group. Always private."
  value       = aws_subnet.private[*].id
}

output "alb_security_group_id" {
  description = "Security group for the load balancer."
  value       = aws_security_group.alb.id
}

output "task_security_group_id" {
  description = "Security group for every ECS task."
  value       = aws_security_group.tasks.id
}

output "data_security_group_id" {
  description = "Security group for RDS, ElastiCache and EFS."
  value       = aws_security_group.data.id
}

output "nat_gateway_count" {
  description = "Number of NAT Gateways created. Each one is a fixed monthly cost."
  value       = local.nat_count
}

output "interface_endpoint_count" {
  description = "Number of interface endpoints created. Each is billed per AZ per hour."
  value       = var.enable_interface_endpoints ? length(var.interface_endpoint_services) : 0
}
