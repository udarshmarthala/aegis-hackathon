# Production composition root.
#
# Thin by design: it wires modules together, creates the encryption key and the
# secret containers, and makes no infrastructure decisions of its own. Anything
# worth arguing about lives in the module that owns it.

data "aws_caller_identity" "current" {}

locals {
  environment = "production"
  name_prefix = "aegis-production"

  # The log group is created by the compute module (it owns the tasks that
  # write to it), but the IAM module needs its ARN to grant write access.
  # Referencing compute's output from iam, and iam's roles from compute, would
  # be a module cycle - so the ARN is constructed here instead. CloudWatch log
  # group ARNs are fully determined by name.
  log_group_name = "/aegis/${local.environment}"
  log_group_arn  = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:${local.log_group_name}"

  # Aegis talks to one vendor and fails over across keys, so the deployment
  # wires one secret container per key rather than one per provider. Slot 1 is
  # "GOOGLE_API_KEY"; the rest are suffixed, matching the application's settings
  # field names exactly - a mismatch here would leave a key provisioned, paid
  # for and never read.
  llm_key_env = [
    for i in range(var.google_api_key_count) :
    i == 0 ? "GOOGLE_API_KEY" : "GOOGLE_API_KEY_${i + 1}"
  ]

  firebase_credential_path = "/tmp/firebase-service-account.json"

  tags = {
    project      = "aegis"
    environment  = local.environment
    owner        = var.owner
    "managed-by" = "terraform"
  }
}

# ----------------------------------------------------------------- kms -----
# One customer-managed key for RDS storage, the RDS-managed master credential
# and ElastiCache. Rotation is on: a key that is never rotated is a key nobody
# can rotate when they need to.
resource "aws_kms_key" "this" {
  description             = "Aegis ${local.environment} encryption key."
  enable_key_rotation     = true
  deletion_window_in_days = 30
  tags                    = merge(local.tags, { component = "security" })
}

resource "aws_kms_alias" "this" {
  name          = "alias/${local.name_prefix}"
  target_key_id = aws_kms_key.this.key_id
}

# ------------------------------------------------------------- secrets ----
# Containers only. Terraform creates the secret; an operator writes the value
# with `aws secretsmanager put-secret-value`. No secret material passes through
# Terraform, so none of it lands in state, in a plan artifact, or in a CI log.
#
# A container with no version is a HARD FAILURE at task start: ECS cannot
# resolve it and the task never runs. That is the intended fail-closed
# behaviour - a control plane running with an unset ingest token would accept
# unauthenticated alerts.
locals {
  core_secret_names = concat(
    ["ALERT_INGEST_TOKEN", "INTERNAL_SIGNING_KEY"],
    local.llm_key_env,
    var.optional_secrets,
  )
}

resource "aws_secretsmanager_secret" "core" {
  for_each = toset(local.core_secret_names)

  name        = "${local.name_prefix}/${lower(replace(each.value, "_", "-"))}"
  description = "Aegis ${local.environment}: ${each.value}. Value set out of band, never by Terraform."
  kms_key_id  = aws_kms_key.this.arn

  # Seven days. A production secret deleted by mistake stays recoverable; the
  # staging root uses a zero-day window instead, because there a week-long name
  # reservation would block every rebuild.
  recovery_window_in_days = 7

  tags = merge(local.tags, { component = "security" })
}

# JSON with two keys because the same password is needed in two shapes: the
# Neo4j container wants NEO4J_AUTH="neo4j/<password>", the application wants
# NEO4J_PASSWORD="<password>". One secret, two ECS JSON-key selectors.
#
#   aws secretsmanager put-secret-value --secret-id aegis-production/neo4j \
#     --secret-string '{"auth":"neo4j/<password>","password":"<password>"}'
resource "aws_secretsmanager_secret" "neo4j" {
  count = var.graph_deployment_mode == "disabled" ? 0 : 1

  name        = "${local.name_prefix}/neo4j"
  description = "Aegis ${local.environment}: Neo4j credentials. JSON with 'auth' and 'password' keys."
  kms_key_id  = aws_kms_key.this.arn
  # Seven-day recovery window, as above.
  recovery_window_in_days = 7

  tags = merge(local.tags, { component = "security" })
}

resource "aws_secretsmanager_secret" "firebase" {
  count = var.enable_firebase ? 1 : 0

  name        = "${local.name_prefix}/firebase-service-account"
  description = "Aegis ${local.environment}: Firebase service account JSON, injected and written to a file at task start."
  kms_key_id  = aws_kms_key.this.arn
  # Seven-day recovery window, as above.
  recovery_window_in_days = 7

  tags = merge(local.tags, { component = "security" })
}

# ------------------------------------------------------------- modules ----
module "network" {
  source = "../../modules/network"

  name_prefix                = local.name_prefix
  vpc_cidr                   = var.vpc_cidr
  az_count                   = 2
  nat_strategy               = var.nat_strategy
  enable_interface_endpoints = var.enable_interface_endpoints
  allowed_ingress_cidrs      = var.allowed_ingress_cidrs
  tags                       = local.tags
}

module "queue" {
  source = "../../modules/queue"

  name_prefix = local.name_prefix
  tags        = local.tags
}

module "data" {
  source = "../../modules/datastores"

  name_prefix       = local.name_prefix
  environment       = local.environment
  subnet_ids        = module.network.database_subnet_ids
  security_group_id = module.network.data_security_group_id
  kms_key_arn       = aws_kms_key.this.arn

  instance_class        = var.db_instance_class
  allocated_storage     = var.db_allocated_storage
  backup_retention_days = var.db_backup_retention_days
  multi_az              = var.db_multi_az
  deletion_protection   = true

  # Enhanced Monitoring at 60s. It is billed per instance per month and it is
  # the only source that shows OS-level contention when the database is the
  # suspect in an incident - which, for this system, it eventually will be.
  monitoring_interval          = 60
  performance_insights_enabled = true
  # Nothing here is disposable. deletion_protection refuses an accidental
  # destroy, the final snapshot is the last line of defence if it is ever
  # forced, and a bucket holding evidence is never emptied by a terraform run.
  skip_final_snapshot   = false
  force_destroy_buckets = false

  enable_redis = var.enable_redis
  tags         = local.tags
}

module "iam" {
  source = "../../modules/iam"

  name_prefix = local.name_prefix
  environment = local.environment

  artifacts_bucket_arn = module.data.artifacts_bucket_arn
  queue_arn            = module.queue.queue_arn
  dlq_arn              = module.queue.dlq_arn
  kms_key_arn          = aws_kms_key.this.arn
  log_group_arn        = local.log_group_arn

  secret_arns = concat(
    [for s in aws_secretsmanager_secret.core : s.arn],
    [module.data.db_master_secret_arn],
    var.graph_deployment_mode == "disabled" ? [] : [aws_secretsmanager_secret.neo4j[0].arn],
    var.enable_firebase ? [aws_secretsmanager_secret.firebase[0].arn] : [],
  )

  observed_cluster_arns     = var.observed_cluster_arns
  allow_remediation_actions = var.allow_remediation_actions

  github_repository    = var.github_repository
  create_oidc_provider = false
  oidc_provider_arn    = var.oidc_provider_arn
  tf_state_bucket_arn  = var.tf_state_bucket_arn
  tf_lock_table_arn    = var.tf_lock_table_arn

  # Environment subjects ONLY. Unlike staging, there is no
  # "ref:refs/heads/main" subject here: a push to main must not be able to
  # assume the production deploy role. GitHub issues an environment subject
  # only for a job that has already cleared that Environment's required
  # reviewers, so protection is enforced by STS, not only by the workflow file.
  deploy_role_subjects = var.github_repository == "" ? [] : [
    "repo:${var.github_repository}:environment:aegis-production",
    "repo:${var.github_repository}:environment:aegis-production-destructive",
  ]

  plan_role_subjects = var.github_repository == "" ? [] : [
    "repo:${var.github_repository}:pull_request",
    "repo:${var.github_repository}:ref:refs/heads/main",
  ]

  tags = local.tags
}

module "compute" {
  source = "../../modules/compute"

  name_prefix = local.name_prefix
  environment = local.environment
  aws_region  = var.aws_region

  vpc_id                 = module.network.vpc_id
  public_subnet_ids      = module.network.public_subnet_ids
  task_subnet_ids        = module.network.task_subnet_ids
  assign_public_ip       = module.network.assign_public_ip
  alb_security_group_id  = module.network.alb_security_group_id
  task_security_group_id = module.network.task_security_group_id
  certificate_arn        = var.certificate_arn

  # Not a variable. Production has no plaintext mode to opt into: certificate_arn
  # is already required above, and hardcoding false here means no tfvars file,
  # no CI input and no operator in a hurry can turn TLS off on the way past.
  allow_insecure_http = false
  alb_logs_bucket     = module.data.alb_logs_bucket_name

  backend_image    = var.backend_image
  image_tag        = var.image_tag
  cpu_architecture = var.cpu_architecture

  api_cpu           = var.api_cpu
  api_memory        = var.api_memory
  api_desired_count = var.api_desired_count

  worker_min_count    = var.worker_min_count
  worker_max_count    = var.worker_max_count
  worker_scaling_mode = var.worker_scaling_mode
  worker_use_spot     = var.worker_use_spot

  queue_name = module.queue.queue_name
  queue_url  = module.queue.queue_url

  execution_role_arn    = module.iam.execution_role_arn
  api_task_role_arn     = module.iam.api_task_role_arn
  worker_task_role_arn  = module.iam.worker_task_role_arn
  migrate_task_role_arn = module.iam.migrate_task_role_arn

  log_retention_days = var.log_retention_days

  environment_variables = merge({
    AEGIS_ENVIRONMENT_NAME = "production"
    LOG_LEVEL              = "INFO"
    POSTGRES_HOST          = module.data.db_address
    POSTGRES_PORT          = tostring(module.data.db_port)
    POSTGRES_DB            = module.data.db_name
    POSTGRES_USER          = module.data.db_username
    REDIS_HOST             = module.data.redis_primary_endpoint
    REDIS_PORT             = "6379"
    NEO4J_URI              = module.graph.neo4j_uri
    NEO4J_USER             = "neo4j"
    CORS_ALLOWED_ORIGINS   = var.cors_allowed_origins
    API_PUBLIC_URL         = var.api_public_url
    GOOGLE_BASE_URL        = var.google_base_url
    LLM_MODEL_FAST         = var.llm_model
    LLM_MODEL_REASONING    = var.llm_model
    LLM_MODEL_CODE         = var.llm_model
    LLM_EMBEDDING_MODEL    = var.llm_embedding_model
    FIREBASE_PROJECT_ID    = var.firebase_project_id

    # Empty unless Firebase is wired in, in which case the task's start wrapper
    # writes the credential file here before the application reads it.
    FIREBASE_SERVICE_ACCOUNT_PATH = var.enable_firebase ? local.firebase_credential_path : ""

    # Fail closed. The backend asserts both of these in production; setting
    # them explicitly means production behaves the same way instead of drifting
    # into a development posture nobody notices until it is promoted.
    AUTH_DEV_MODE    = "false"
    AUTONOMY_ENABLED = "false"

    WORKLOAD_ADAPTER  = "ecs"
    LANGSMITH_TRACING = contains(var.optional_secrets, "LANGSMITH_API_KEY") ? "true" : "false"
  }, var.extra_environment_variables)

  secret_arns = merge(
    { for name, s in aws_secretsmanager_secret.core : name => s.arn },
    # ECS selects a field from a JSON secret with "<arn>:<key>::". The RDS
    # master secret is {"username":...,"password":...}.
    { POSTGRES_PASSWORD = "${module.data.db_master_secret_arn}:password::" },
    var.graph_deployment_mode == "disabled" ? {} : {
      NEO4J_PASSWORD = "${aws_secretsmanager_secret.neo4j[0].arn}:password::"
    },
  )

  firebase_secret_arn      = var.enable_firebase ? aws_secretsmanager_secret.firebase[0].arn : ""
  firebase_credential_path = local.firebase_credential_path

  tags = local.tags
}

module "graph" {
  source = "../../modules/graph"

  deployment_mode = var.graph_deployment_mode
  name_prefix     = local.name_prefix
  aws_region      = var.aws_region

  vpc_id                 = module.network.vpc_id
  subnet_ids             = module.network.task_subnet_ids
  task_security_group_id = module.network.task_security_group_id
  efs_security_group_id  = module.network.data_security_group_id
  assign_public_ip       = module.network.assign_public_ip

  cluster_arn        = module.compute.ecs_cluster_arn
  execution_role_arn = module.iam.execution_role_arn
  task_role_arn      = module.iam.graph_task_role_arn
  log_group_name     = module.compute.log_group_name

  # The Neo4j container wants NEO4J_AUTH="neo4j/<password>"; the application
  # wants the bare password. Same secret, two JSON keys.
  password_secret_arn = var.graph_deployment_mode == "disabled" ? "" : "${aws_secretsmanager_secret.neo4j[0].arn}:auth::"
  neo4j_external_uri  = var.neo4j_external_uri

  tags = local.tags
}

module "observability" {
  source = "../../modules/observability"

  name_prefix = local.name_prefix
  environment = local.environment

  alarm_emails     = var.alarm_emails
  budget_limit_usd = var.budget_limit_usd

  alb_arn_suffix          = module.compute.alb_arn_suffix
  target_group_arn_suffix = module.compute.target_group_arn_suffix
  db_instance_id          = module.data.db_instance_id
  ecs_cluster_name        = module.compute.ecs_cluster_name
  api_service_name        = module.compute.api_service_name
  worker_service_name     = module.compute.worker_service_name
  queue_name              = module.queue.queue_name
  dlq_name                = module.queue.dlq_name

  tags = local.tags
}
