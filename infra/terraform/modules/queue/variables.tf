variable "name_prefix" {
  description = "Prefix for every resource name, e.g. aegis-staging."
  type        = string
}

variable "visibility_timeout_seconds" {
  description = <<-EOT
    How long a received message is hidden from other consumers.

    This must exceed the longest investigation an agent can run. The supervisor
    budget (AGENT_MAX_WALL_SECONDS) defaults to 600s, so 900s leaves headroom
    for startup and checkpointing. Set it too low and a slow investigation is
    redelivered while the first one is still running - two agents, one
    incident, two sets of proposed actions.
  EOT
  type        = number
  default     = 900
}

variable "message_retention_seconds" {
  description = "How long an undelivered message survives. Four days: long enough to survive a weekend outage."
  type        = number
  default     = 345600
}

variable "max_receive_count" {
  description = <<-EOT
    Deliveries before a message is moved to the dead-letter queue.

    Three, not ten. A poison message that crashes the worker should stop
    crashing the worker quickly; ten attempts at a 15-minute visibility timeout
    is two and a half hours of a worker looping on a message it will never
    process.
  EOT
  type        = number
  default     = 3
}

variable "receive_wait_time_seconds" {
  description = "Long-poll duration. 20 is the maximum and costs the fewest empty receives."
  type        = number
  default     = 20
}

variable "dlq_retention_seconds" {
  description = "Dead-letter retention. Fourteen days: a failure found on Monday is still inspectable."
  type        = number
  default     = 1209600
}

variable "tags" {
  description = "Tags applied to every resource in this module."
  type        = map(string)
  default     = {}
}
