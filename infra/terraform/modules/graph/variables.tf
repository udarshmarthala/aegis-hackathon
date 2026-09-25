variable "deployment_mode" {
  description = <<-EOT
    How Neo4j is provided.

      "ecs_fargate" - a single Neo4j Community task on Fargate with an EFS
                      volume for persistence, reachable only inside the VPC
                      through Cloud Map service discovery. ~$30-35/month.
      "external"    - provision nothing. Point at a managed instance (Neo4j
                      AuraDB) via neo4j_external_uri and a secret holding the
                      credentials.
      "disabled"    - no graph at all. Aegis records an evidence gap and
                      continues with lower confidence; it does not fail.

    The single-task Fargate deployment is not highly available and that is a
    deliberate match to the data's criticality. Neo4j holds TOPOLOGY, not the
    system of record (CLAUDE.md invariant 10). Losing it degrades confidence
    and records an evidence gap. Paying for an HA graph cluster to protect
    derived data, while the authoritative Postgres runs single-AZ, would be
    spending money in the wrong place.
  EOT
  type        = string
  default     = "ecs_fargate"

  validation {
    condition     = contains(["ecs_fargate", "external", "disabled"], var.deployment_mode)
    error_message = "deployment_mode must be one of: ecs_fargate, external, disabled."
  }
}

variable "name_prefix" {
  description = "Prefix for every resource name."
  type        = string
}

variable "vpc_id" {
  description = "VPC id. Required for the service discovery namespace."
  type        = string
}

variable "subnet_ids" {
  description = "Subnets for the Neo4j task and the EFS mount targets."
  type        = list(string)
}

variable "task_security_group_id" {
  description = "Security group for the Neo4j task."
  type        = string
}

variable "efs_security_group_id" {
  description = "Security group for the EFS mount targets (NFS from tasks only)."
  type        = string
}

variable "assign_public_ip" {
  description = "Whether the task needs a public IP (true only when the network module runs tasks in public subnets)."
  type        = bool
  default     = false
}

variable "cluster_arn" {
  description = "ECS cluster ARN to run the Neo4j service in."
  type        = string
  default     = ""
}

variable "execution_role_arn" {
  description = "ECS task execution role: pulls the image, reads the password secret, writes logs."
  type        = string
  default     = ""
}

variable "task_role_arn" {
  description = "Task role for the Neo4j container. Neo4j calls no AWS API; this is intentionally near-empty."
  type        = string
  default     = ""
}

variable "log_group_name" {
  description = "CloudWatch log group the container writes to."
  type        = string
  default     = ""
}

variable "aws_region" {
  description = "Region, for the awslogs driver configuration."
  type        = string
}

variable "image" {
  description = "Neo4j image. Community edition: Aegis uses no Enterprise feature."
  type        = string
  default     = "neo4j:5.26-community"
}

variable "cpu" {
  description = "Fargate CPU units. 512 = 0.5 vCPU."
  type        = number
  default     = 512
}

variable "memory" {
  description = "Fargate memory in MiB. Heap and page cache below must fit inside this."
  type        = number
  default     = 2048
}

variable "heap_max_size" {
  description = "Neo4j JVM heap. Kept well under the task memory so the page cache and JVM overhead fit."
  type        = string
  default     = "768m"
}

variable "pagecache_size" {
  description = "Neo4j page cache."
  type        = string
  default     = "512m"
}

variable "password_secret_arn" {
  description = "Secrets Manager ARN holding the Neo4j password (plain string, not JSON)."
  type        = string
  default     = ""
}

variable "neo4j_external_uri" {
  description = "bolt+s:// URI when deployment_mode is external."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Tags applied to every resource in this module."
  type        = map(string)
  default     = {}
}
