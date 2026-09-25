output "enabled" {
  description = "Whether a graph endpoint is configured at all."
  value       = var.deployment_mode != "disabled"
}

output "neo4j_uri" {
  description = <<-EOT
    Bolt URI for the API and worker. Empty when the graph is disabled, in which
    case Aegis records an evidence gap rather than failing to start.
  EOT
  value = (
    var.deployment_mode == "ecs_fargate"
    ? "bolt://neo4j.${local.namespace}:7687"
    : (var.deployment_mode == "external" ? var.neo4j_external_uri : "")
  )
}

output "service_name" {
  description = "ECS service name, or an empty string when Neo4j is not self-hosted."
  value       = local.self ? aws_ecs_service.neo4j[0].name : ""
}

output "namespace_id" {
  description = "Cloud Map namespace id, for other in-VPC services that need internal DNS."
  value       = local.self ? aws_service_discovery_private_dns_namespace.this[0].id : ""
}

output "efs_file_system_id" {
  description = "EFS file system holding the graph store."
  value       = local.self ? aws_efs_file_system.this[0].id : ""
}
