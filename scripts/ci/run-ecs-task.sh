#!/usr/bin/env bash
# Run a one-off ECS Fargate task to completion and fail on a non-zero exit.
#
# `aws ecs run-task` returns as soon as the task is accepted. Without the wait
# and the exit-code check below, a failed migration would look like a
# successful deploy step — exactly the silent failure this platform exists to
# catch in other people's systems.
#
# Usage:
#   run-ecs-task.sh --cluster C --task-definition F --subnets a,b --security-groups sg
#                   [--log-group G] [--log-prefix P] [--container NAME]
#                   [--timeout-seconds N] [-- command args...]
set -euo pipefail

CLUSTER="" TASKDEF="" SUBNETS="" SGS="" LOG_GROUP="" LOG_PREFIX=""
CONTAINER="" TIMEOUT=900
COMMAND_OVERRIDE=()

while [ $# -gt 0 ]; do
  case "$1" in
    --cluster)          CLUSTER="$2"; shift 2 ;;
    --task-definition)  TASKDEF="$2"; shift 2 ;;
    --subnets)          SUBNETS="$2"; shift 2 ;;
    --security-groups)  SGS="$2"; shift 2 ;;
    --log-group)        LOG_GROUP="$2"; shift 2 ;;
    --log-prefix)       LOG_PREFIX="$2"; shift 2 ;;
    --container)        CONTAINER="$2"; shift 2 ;;
    --timeout-seconds)  TIMEOUT="$2"; shift 2 ;;
    --)                 shift; COMMAND_OVERRIDE=("$@"); break ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

: "${CLUSTER:?--cluster is required}"
: "${TASKDEF:?--task-definition is required}"
: "${SUBNETS:?--subnets is required}"
: "${SGS:?--security-groups is required}"

network="awsvpcConfiguration={subnets=[${SUBNETS}],securityGroups=[${SGS}],assignPublicIp=DISABLED}"

overrides="{}"
if [ "${#COMMAND_OVERRIDE[@]}" -gt 0 ]; then
  : "${CONTAINER:?--container is required when overriding the command}"
  # --args goes AFTER the filter: jq treats the first non-option argument as
  # the filter, so leading with --args would swallow it.
  overrides="$(jq -nc --arg name "${CONTAINER}" \
    '{containerOverrides: [{name: $name, command: $ARGS.positional}]}' \
    --args "${COMMAND_OVERRIDE[@]}")"
fi

echo "Starting one-off task ${TASKDEF} on ${CLUSTER}"
task_arn="$(aws ecs run-task \
  --cluster "${CLUSTER}" \
  --task-definition "${TASKDEF}" \
  --launch-type FARGATE \
  --count 1 \
  --network-configuration "${network}" \
  --overrides "${overrides}" \
  --started-by "github-${GITHUB_RUN_ID:-local}" \
  --query 'tasks[0].taskArn' --output text)"

if [ -z "${task_arn}" ] || [ "${task_arn}" = "None" ]; then
  echo "run-task returned no task ARN; check the failures array above." >&2
  exit 1
fi
task_id="${task_arn##*/}"
echo "task: ${task_id}"

# aws ecs wait tasks-stopped polls for up to 100 attempts at 6s (~10 minutes).
# A longer migration needs the explicit loop below rather than the waiter.
deadline=$(( $(date +%s) + TIMEOUT ))
status=""
while [ "$(date +%s)" -lt "${deadline}" ]; do
  status="$(aws ecs describe-tasks --cluster "${CLUSTER}" --tasks "${task_arn}" \
    --query 'tasks[0].lastStatus' --output text)"
  [ "${status}" = "STOPPED" ] && break
  sleep 10
done

if [ "${status}" != "STOPPED" ]; then
  echo "task did not stop within ${TIMEOUT}s (last status: ${status}); stopping it." >&2
  aws ecs stop-task --cluster "${CLUSTER}" --task "${task_arn}" \
    --reason "timed out in CI" >/dev/null || true
  exit 1
fi

# Logs before the verdict: a failing migration's stack trace is the only thing
# anyone reading this job actually wants.
if [ -n "${LOG_GROUP}" ] && [ -n "${LOG_PREFIX}" ] && [ -n "${CONTAINER}" ]; then
  stream="${LOG_PREFIX}/${CONTAINER}/${task_id}"
  echo "--- logs (${LOG_GROUP}:${stream}) ---"
  aws logs get-log-events --log-group-name "${LOG_GROUP}" \
    --log-stream-name "${stream}" --limit 500 --start-from-head \
    --query 'events[].message' --output text 2>/dev/null || \
    echo "(log stream not available)"
  echo "--- end logs ---"
fi

read -r exit_code reason <<<"$(aws ecs describe-tasks --cluster "${CLUSTER}" \
  --tasks "${task_arn}" \
  --query 'tasks[0].containers[0].[exitCode,reason]' --output text)"

if [ "${exit_code}" != "0" ]; then
  echo "::error title=One-off task failed::${TASKDEF} exited ${exit_code} (${reason:-no reason reported})"
  exit 1
fi
echo "task ${task_id} completed with exit code 0"
