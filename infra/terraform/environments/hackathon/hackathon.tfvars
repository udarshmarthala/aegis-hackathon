# Non-secret values for the hackathon environment. NO SECRETS IN THIS FILE:
# every credential lives in the Secrets Manager secret aegis-hackathon/app,
# written out of band (docs/AWS_HACKATHON_ARCHITECTURE.md, "Bring-up").
#
# The repository's .gitignore excludes *.tfvars; this one is meant to be
# committed, so add it with `git add -f` (or add a negation rule for it).
#
#   AWS_PROFILE=aegis-admin terraform plan -var-file=hackathon.tfvars -out=tfplan
#   AWS_PROFILE=aegis-admin terraform apply tfplan

aws_region = "us-west-2"
owner      = "udarshmarthala"

# The Vercel production alias (repository homepage; answered 200 on 2026-09-26).
# Exact origin, no wildcard: the production validator refuses one.
cors_allowed_origins = "https://aegis-hackathon-kohl.vercel.app"

# From the deployed frontend bundle: authDomain aegis-ai-detective.firebaseapp.com.
# Confirm it is the project id in the Firebase console before apply.
firebase_project_id = "aegis-ai-detective"
enable_firebase     = true

aegis_env = "production"

# FIRST apply: no image exists at "bootstrap", so both services start at 0 and
# the first deploy-hackathon run (bring_up = true) starts them. After that run,
# set both counts to 1 and image_tag to the deployed commit SHA, then re-apply.
image_tag            = "2ea5cf539792e92bde5a7194f0801640147f0567"
api_desired_count    = 1
worker_desired_count = 1

cpu_architecture = "ARM64"
api_cpu          = 512
api_memory       = 1024
worker_cpu       = 512
worker_memory    = 1024

# false for the judging window (a Spot interruption stalls an incident for up
# to 15 minutes until app change A3 lands); true afterwards saves ~$9/month.
worker_use_spot = true

db_instance_class        = "db.t4g.micro"
db_allocated_storage     = 20
db_backup_retention_days = 3
db_deletion_protection   = false
db_skip_final_snapshot   = true
force_destroy_buckets    = true
log_retention_days       = 7

bedrock_model_id          = "us.anthropic.claude-sonnet-5"
bedrock_fallback_model_id = "us.anthropic.claude-sonnet-4-6"

# Keys that MUST exist in the aegis-hackathon/app JSON secret, or ECS refuses to
# start the task. Remove an integration from BOTH lists if you have no key.
api_secret_keys    = ["ALERT_INGEST_TOKEN", "GOOGLE_API_KEY", "GOOGLE_API_KEY_2", "GOOGLE_API_KEY_3", "GOOGLE_API_KEY_4", "GOOGLE_API_KEY_5", "GOOGLE_API_KEY_6", "GOOGLE_API_KEY_7", "GOOGLE_API_KEY_8", "RAWTREE_READ_KEY", "NIMBLE_API_KEY", "BFL_API_KEY", "LANGSMITH_API_KEY"]
worker_secret_keys = ["ALERT_INGEST_TOKEN", "GOOGLE_API_KEY", "GOOGLE_API_KEY_2", "GOOGLE_API_KEY_3", "GOOGLE_API_KEY_4", "GOOGLE_API_KEY_5", "GOOGLE_API_KEY_6", "GOOGLE_API_KEY_7", "GOOGLE_API_KEY_8", "RAWTREE_WRITE_KEY", "RAWTREE_READ_KEY", "NIMBLE_API_KEY", "BFL_API_KEY", "LANGSMITH_API_KEY"]

neo4j_uri = ""

github_repository           = "udarshmarthala/aegis-hackathon"
github_environment          = "aegis-hackathon"
github_immutable_sub_prefix = "repo:udarshmarthala@103487639/aegis-hackathon@1388138462"
create_github_oidc_provider = true

budget_limit_usd = 100
# Add an address to receive 50/80/100% alerts (each must confirm by email).
budget_emails = []
