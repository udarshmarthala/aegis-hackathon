#!/usr/bin/env bash
# Print the image tag an ECS service is currently running.
#
# A pull-request `terraform plan` should show the infrastructure diff, not a
# phantom image change caused by defaulting image_tag to the PR's own SHA. This
# resolves the live tag so the plan is honest about what is actually changing.
#
# Prints "bootstrap" when the cluster, service or task definition does not
# exist yet — the first deploy into an empty account is not an error.
#
# Usage: current-image-tag.sh <cluster> <service>
set -euo pipefail

CLUSTER="${1:?usage: current-image-tag.sh <cluster> <service>}"
SERVICE="${2:?usage: current-image-tag.sh <cluster> <service>}"

taskdef="$(aws ecs describe-services --cluster "${CLUSTER}" --services "${SERVICE}" \
  --query 'services[0].taskDefinition' --output text 2>/dev/null || true)"

if [ -z "${taskdef}" ] || [ "${taskdef}" = "None" ]; then
  echo "bootstrap"
  exit 0
fi

image="$(aws ecs describe-task-definition --task-definition "${taskdef}" \
  --query 'taskDefinition.containerDefinitions[0].image' --output text 2>/dev/null || true)"

if [ -z "${image}" ] || [ "${image}" = "None" ]; then
  echo "bootstrap"
  exit 0
fi

# <account>.dkr.ecr.<region>.amazonaws.com/<repo>:<tag> -> <tag>
case "${image}" in
  *:*) echo "${image##*:}" ;;
  *)   echo "bootstrap" ;;
esac
