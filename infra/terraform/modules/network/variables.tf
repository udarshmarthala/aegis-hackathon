variable "name_prefix" {
  description = "Prefix for every resource name, e.g. aegis-staging."
  type        = string
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC. /20 leaves room for 2-3 AZs without being wasteful."
  type        = string
  default     = "10.40.0.0/20"
}

variable "az_count" {
  description = <<-EOT
    Number of availability zones. Two is the minimum an ALB accepts.
    Three costs more in NAT and interface endpoints for availability this
    deployment does not need.
  EOT
  type        = number
  default     = 2

  validation {
    condition     = var.az_count >= 2 && var.az_count <= 3
    error_message = "az_count must be 2 or 3: an ALB requires at least two subnets."
  }
}

variable "nat_strategy" {
  description = <<-EOT
    How tasks reach the internet. This is the single largest fixed-cost lever
    in the whole deployment; the reasoning is in docs/cost-strategy.md.

      "single"      - one NAT Gateway shared by every private subnet.
                      ~$33/month plus data processing. Tasks have no public
                      IP and nothing on the internet can address them.
                      An AZ failure takes egress with it.
      "per_az"      - one NAT Gateway per AZ. ~$33/month each. Removes the
                      egress single point of failure.
      "none_public" - no NAT. Tasks run in public subnets with public IPs and
                      are protected only by security groups. Saves the NAT
                      cost entirely and is a MATERIALLY WEAKER posture: a
                      security group misconfiguration becomes internet
                      exposure instead of nothing. Acceptable for a cost-
                      constrained staging environment, never the default.

    Aegis calls external LLM APIs, LangSmith, GitHub and Slack, so outbound
    internet access is a functional requirement. VPC interface endpoints
    cannot replace it - they only cover AWS service traffic.
  EOT
  type        = string
  default     = "single"

  validation {
    condition     = contains(["single", "per_az", "none_public"], var.nat_strategy)
    error_message = "nat_strategy must be one of: single, per_az, none_public."
  }
}

variable "enable_interface_endpoints" {
  description = <<-EOT
    Create interface (PrivateLink) endpoints for ECR, CloudWatch Logs, SQS,
    Secrets Manager and SSM.

    Default false and that is deliberate. Each interface endpoint costs about
    $7.30 per AZ per month; five of them across two AZs is roughly $73/month,
    which is MORE than the single NAT Gateway ($33/month) they would be
    offsetting at this traffic volume. They become worth it when NAT data
    processing charges exceed the endpoint cost - roughly 800GB/month of ECR
    and log traffic - or when a compliance requirement demands that AWS API
    traffic never traverse a NAT.

    The S3 and DynamoDB GATEWAY endpoints below are always created: they are
    free, and they keep ECR layer downloads off the NAT.
  EOT
  type        = bool
  default     = false
}

variable "interface_endpoint_services" {
  description = "Service short names for interface endpoints when enabled."
  type        = list(string)
  default = [
    "ecr.api",
    "ecr.dkr",
    "logs",
    "sqs",
    "secretsmanager",
  ]
}

variable "allowed_ingress_cidrs" {
  description = <<-EOT
    CIDRs allowed to reach the ALB. Defaults to the whole internet because the
    control plane is served to operators and to a Vercel-hosted frontend whose
    egress addresses are not stable. Narrow this wherever you can.
  EOT
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "tags" {
  description = "Tags applied to every resource in this module."
  type        = map(string)
  default     = {}
}
