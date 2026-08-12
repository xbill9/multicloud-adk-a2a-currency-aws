#!/usr/bin/env bash
# Deploy the AWS leg: the Strands/a2a-sdk agent on Bedrock AgentCore Runtime.
#
#   ./infra/deploy_aws.sh deploy        # ECR, ARM64 image, runtime, exec role
#   ./infra/deploy_aws.sh env           # env vars to add to the master
#   ./infra/deploy_aws.sh verify        # negative controls -- run these
#   ./infra/deploy_aws.sh scope-test    # answers open question 2
#   ./infra/deploy_aws.sh url
#   ./infra/deploy_aws.sh destroy
#
# AgentCore Runtime, not Lambda. An agent runtime is where an AWS agent goes,
# and hosting this one on generic compute would make the mesh two agent
# runtimes and a function -- which concedes the premise the article rests on,
# and breaks comparability with the predecessor series' 18.8-25.1s
# hosted-runtime measurements.
#
# The A2A protocol contract (port 9000, root path, ARM64, GET /ping) is
# satisfied by infra/Dockerfile.aws and agents/serving.py unmodified.
#
# **There is no federated role here any more.** While the master ran on Cloud
# Run this script also created `currency-aws-federated`, a role trusting
# `accounts.google.com` natively, which the master assumed with
# AssumeRoleWithWebIdentity. The master now runs on AgentCore in this same
# account, so it signs with its own execution role and the permission lives
# there -- see ensure_exec_role in deploy_master_aws.sh. That makes this leg the
# cheapest in the mesh and also an **in-cloud hop**, which is labelled as such
# everywhere it is reported rather than left to pad the interop claim.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGION="${AWS_REGION:-us-west-2}"
RUNTIME="${RUNTIME:-currency_aws}"
ECR_REPO="${ECR_REPO:-currency-mesh}"
ROLE_EXEC="${ROLE_EXEC:-currency-aws-agentcore-exec}"
# Overridable so the script works before a fresh login picks up the docker
# group: DOCKER="sudo -u $USER -g docker docker" ./infra/deploy_aws.sh deploy
DOCKER="${DOCKER:-docker}"

# `llm` mode's model, and the two ARNs the exec role needs to invoke it. A
# cross-region inference profile (the `us.` prefix) is not itself sufficient:
# the grant must name both the profile in this account and the underlying
# foundation model, which is regionless in the ARN. Scoped to this one model
# rather than "*", consistent with the least-privilege finding for
# InvokeAgentRuntime in docs/DEPLOYMENT_PLAN.md.
BEDROCK_MODEL_ID="${BEDROCK_MODEL_ID:-us.amazon.nova-micro-v1:0}"
BEDROCK_MODEL_BASE="${BEDROCK_MODEL_BASE:-${BEDROCK_MODEL_ID#us.}}"

# `direct` stays the default: the matrix is a protocol instrument first, and a
# model in the path turns a red cell into two possible explanations. Set
# MODEL_MODE=llm to deploy the brain, and set it back afterwards.
MODEL_MODE="${MODEL_MODE:-direct}"

# Pinned rather than minted per call: each AgentCore session gets its own
# microVM, and a cold one is worth several seconds. That cost was once measured,
# attributed to the client stack, written up as an anomaly, and then retracted.
SESSION_ID="${AWS_A2A_SESSION_ID:-currency-mesh-master-to-aws-session-0001}"

account_id() { aws sts get-caller-identity --query Account --output text; }

# Credential preflight, run before anything that would fail confusingly without
# it. Be clear about what this can and cannot do, because the obvious reading
# is wrong in two ways.
#
# save-aws-creds.sh runs `aws configure export-credentials`, which *exports
# credentials that already exist*. It cannot refresh a dead session -- when the
# session expired on 2026-08-09 the script failed too. So this is capture and
# re-inject, never a retry that conjures a session.
#
# And it cannot rescue a shell whose AWS_* environment variables are wrong:
# environment sits at the top of the AWS credential chain, so export-credentials
# would faithfully re-export the same broken values. Measured, not assumed --
# that case was written first and then failed its own test.
#
# What is left is narrow but real:
#
#   1. ambient credentials work         -> proceed
#   2. no usable ambient credentials,
#      but $CREDS_FILE holds live ones  -> source it, proceed
#   3. neither                          -> only `aws login` fixes it; say so
#
# Case 2 is the one that earns its place: a fresh shell, or a cron/CI context
# with no profile, running against credentials captured earlier.
CREDS_FILE="${CREDS_FILE:-${REPO}/.aws_creds}"

ensure_aws_credentials() {
  aws sts get-caller-identity >/dev/null 2>&1 && return 0

  echo "aws credentials are not usable in this shell; trying save-aws-creds.sh" >&2
  if [[ -x "$REPO/save-aws-creds.sh" ]] \
     && (cd "$REPO" && ./save-aws-creds.sh "$CREDS_FILE" >/dev/null 2>&1); then
    # shellcheck disable=SC1090
    set -a; . "$CREDS_FILE"; set +a
    if aws sts get-caller-identity >/dev/null 2>&1; then
      echo "recovered from ${CREDS_FILE}" >&2
      return 0
    fi
  fi

  # A previously captured file is the last resort: it may hold credentials from
  # a session that is still valid even though this shell cannot see them.
  if [[ -r "$CREDS_FILE" ]]; then
    # shellcheck disable=SC1090
    set -a; . "$CREDS_FILE"; set +a
    if aws sts get-caller-identity >/dev/null 2>&1; then
      echo "recovered from the existing ${CREDS_FILE}" >&2
      return 0
    fi
  fi

  echo "error: no usable AWS credentials, and save-aws-creds.sh cannot make any." >&2
  echo "       the session itself is gone -- re-authenticate, then re-run:" >&2
  echo "         aws login          (or: aws sso login)" >&2
  return 1
}

runtime_arn() {
  # Retried because bedrock-agentcore-control intermittently fails this call
  # with `ValidationException ... CreateOAuth2Token ... the provided
  # authorization grant is invalid, expired, revoked, or malformed` on a
  # session that is demonstrably valid -- `aws sts get-caller-identity`
  # succeeds either side of it, and the very next attempt returns the ARN.
  # Seen twice on 2026-08-08. Left unhandled it reads exactly like an expired
  # session, which is the wrong diagnosis and the expensive one: the second
  # occurrence aborted a matrix deploy mid-measurement.
  # Backoff rather than a fixed delay: a first attempt at 3x3s failed a wire
  # step whose very next manual retry, seconds later, succeeded -- so the
  # outage window is sometimes longer than the whole retry budget was.
  # Budgeted at ~2 minutes rather than a fixed attempt count: 3x3s was too
  # short, 5 attempts over 45s was still too short, and each time the very next
  # manual call succeeded. The windows are minutes, not seconds.
  # Retry only when the *call* fails. A call that succeeds and reports no such
  # runtime is a fact, not a transient, and retrying it spent two minutes on
  # every first deploy waiting for an answer that had already arrived.
  local attempt arn rc delay=3
  for attempt in 1 2 3 4 5 6 7; do
    rc=0
    arn="$(aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" \
      --query "agentRuntimes[?agentRuntimeName=='${RUNTIME}'].agentRuntimeArn | [0]" \
      --output text 2>/dev/null)" || rc=$?

    if [[ "$rc" -eq 0 ]]; then
      [[ "$attempt" -gt 1 ]] && echo "note: runtime ARN resolved on attempt ${attempt}" >&2
      # May legitimately be "None": the caller distinguishes absent from failed.
      echo "${arn:-None}"
      return 0
    fi
    [[ "$attempt" -lt 7 ]] && { sleep "$delay"; [[ "$delay" -lt 30 ]] && delay=$((delay * 2)); }
  done
  return 1
}

# AgentCore mounts the A2A server under a path built from the URL-encoded ARN.
# The card lives beneath the same path, which is why discovery is privileged
# here in exactly the way it is on Cloud Run: one resource, one authorization.
runtime_url() {
  local arn escaped
  arn="$(runtime_arn)" || arn=""
  # An empty ARN has two causes and they need different fixes: the runtime is
  # genuinely absent, or the call never reached AWS. Naming only the first sent
  # this exact session looking for a deleted runtime that was in fact READY.
  [[ "$arn" == "None" || -z "$arn" ]] && {
    echo "could not resolve the '${RUNTIME}' runtime ARN in ${REGION}." >&2
    echo "  either it is not deployed, or this shell cannot reach AWS." >&2
    echo "  check which: aws sts get-caller-identity" >&2
    exit 1
  }
  escaped="$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1],safe=""))' "$arn")"
  echo "https://bedrock-agentcore.${REGION}.amazonaws.com/runtimes/${escaped}/invocations/"
}

build_and_push() {
  local account image
  account="$(account_id)"
  image="${account}.dkr.ecr.${REGION}.amazonaws.com/${ECR_REPO}:aws-agent"

  # Everything here goes to stderr except the final echo. The caller does
  # image="$(build_and_push)", so a single stray stdout line -- docker login's
  # "Login Succeeded", every push layer -- ends up concatenated into the image
  # URI and is then passed to create-agent-runtime as a containerUri. That
  # fails in a way that names neither docker nor the capture.
  {
    aws ecr describe-repositories --repository-names "$ECR_REPO" --region "$REGION" \
      >/dev/null 2>&1 || \
      aws ecr create-repository --repository-name "$ECR_REPO" --region "$REGION" >/dev/null

    aws ecr get-login-password --region "$REGION" \
      | $DOCKER login --username AWS --password-stdin \
          "${account}.dkr.ecr.${REGION}.amazonaws.com"

    # ARM64 is required by AgentCore, not preferred. On an x86 host this needs
    # buildx plus qemu (binfmt_misc must be mounted); a silently-amd64 image is
    # accepted by ECR and then fails at runtime with an exec-format error
    # naming neither the architecture nor the image.
    $DOCKER buildx build --platform linux/arm64 --load \
      -f "$REPO/infra/Dockerfile.aws" -t "$image" "$REPO"
    $DOCKER push "$image"
  } >&2

  echo "$image"
}

ensure_exec_role() {
  local account created=0; account="$(account_id)"

  # The inline policy is re-applied on every run, not just at creation. It used
  # to return early when the role existed, which meant any later addition --
  # such as the Bedrock grant below, without which `llm` mode 403s -- silently
  # never reached an already-deployed role. put-role-policy overwrites, so this
  # is idempotent.
  if ! aws iam get-role --role-name "$ROLE_EXEC" >/dev/null 2>&1; then
    created=1
    aws iam create-role --role-name "$ROLE_EXEC" \
    --assume-role-policy-document "{
      \"Version\": \"2012-10-17\",
      \"Statement\": [{
        \"Effect\": \"Allow\",
        \"Principal\": {\"Service\": \"bedrock-agentcore.amazonaws.com\"},
        \"Action\": \"sts:AssumeRole\",
        \"Condition\": {\"StringEquals\": {\"aws:SourceAccount\": \"${account}\"}}
      }]
    }" >/dev/null
  fi

  aws iam put-role-policy --role-name "$ROLE_EXEC" \
    --policy-name agentcore-runtime \
    --policy-document "{
      \"Version\": \"2012-10-17\",
      \"Statement\": [
        {
          \"Effect\": \"Allow\",
          \"Action\": [
            \"ecr:GetAuthorizationToken\",
            \"ecr:BatchGetImage\",
            \"ecr:GetDownloadUrlForLayer\"
          ],
          \"Resource\": \"*\"
        },
        {
          \"Effect\": \"Allow\",
          \"Action\": [
            \"logs:CreateLogGroup\",
            \"logs:CreateLogStream\",
            \"logs:PutLogEvents\"
          ],
          \"Resource\": \"arn:aws:logs:${REGION}:${account}:*\"
        },
        {
          \"Effect\": \"Allow\",
          \"Action\": [\"bedrock:InvokeModel\", \"bedrock:InvokeModelWithResponseStream\"],
          \"Resource\": [
            \"arn:aws:bedrock:*::foundation-model/${BEDROCK_MODEL_BASE}\",
            \"arn:aws:bedrock:${REGION}:${account}:inference-profile/${BEDROCK_MODEL_ID}\"
          ]
        }
      ]
    }"
  if [[ "$created" == 1 ]]; then
    echo "waiting for ${ROLE_EXEC} to propagate"
    sleep 15
  fi
}

deploy() {
  local image account arn url
  image="$(build_and_push)"
  account="$(account_id)"
  ensure_exec_role

  # `|| arn=""` is load-bearing. runtime_arn returns non-zero when no runtime
  # exists, which is exactly the state of a first deploy, and an unguarded
  # assignment under `set -e` exits the script here -- silently, before
  # create-agent-runtime is ever reached. Found by tearing the leg down and
  # rebuilding it: the create path had been broken by a change that only the
  # update path exercised.
  arn="$(runtime_arn)" || arn=""
  if [[ "$arn" == "None" || -z "$arn" ]]; then
    aws bedrock-agentcore-control create-agent-runtime --region "$REGION" \
      --agent-runtime-name "$RUNTIME" \
      --agent-runtime-artifact "containerConfiguration={containerUri=${image}}" \
      --role-arn "arn:aws:iam::${account}:role/${ROLE_EXEC}" \
      --network-configuration 'networkMode=PUBLIC' \
      --protocol-configuration 'serverProtocol=A2A' >/dev/null
    # --authorizer-configuration is OMITTED, which is what selects SigV4.
    # It is a tagged union whose only member is customJWTAuthorizer, so an
    # empty '{}' is rejected with "Must set one of the following keys for
    # tagged union structure authorizerConfiguration" -- an error that reads
    # like the field is required when in fact it must be absent.
  else
    aws bedrock-agentcore-control update-agent-runtime --region "$REGION" \
      --agent-runtime-id "$(basename "$arn")" \
      --agent-runtime-artifact "containerConfiguration={containerUri=${image}}" \
      --role-arn "arn:aws:iam::${account}:role/${ROLE_EXEC}" \
      --network-configuration 'networkMode=PUBLIC' \
      --protocol-configuration 'serverProtocol=A2A' >/dev/null
  fi

  # After a create the runtime should resolve, but it may not be listable for a
  # moment; runtime_arn already retries. Fail loudly rather than under set -e.
  arn="$(runtime_arn)" || {
    echo "error: runtime was created but its ARN did not resolve." >&2
    echo "       re-run deploy; the create is idempotent from here." >&2
    return 1
  }
  url="$(runtime_url)"

  # Two-phase by necessity: the card must advertise the invocations URL, which
  # is derived from an ARN that does not exist until the runtime does.
  aws bedrock-agentcore-control update-agent-runtime --region "$REGION" \
    --agent-runtime-id "$(basename "$arn")" \
    --agent-runtime-artifact "containerConfiguration={containerUri=${image}}" \
    --role-arn "arn:aws:iam::${account}:role/${ROLE_EXEC}" \
    --network-configuration 'networkMode=PUBLIC' \
    --protocol-configuration 'serverProtocol=A2A' \
    --environment-variables "PUBLIC_URL=${url},CURRENCY_MODEL_MODE=${MODEL_MODE},BEDROCK_MODEL_ID=${BEDROCK_MODEL_ID},HOST=0.0.0.0,PORT=9000" \
    >/dev/null

  echo
  echo "runtime arn : $arn"
  echo "runtime url : $url"
  echo
  echo "Now run: ./infra/deploy_aws.sh verify"
}

env_block() {
  # Resolve and validate before emitting anything.
  #
  # These used to be command substitutions inside the heredoc below, which is
  # a subshell: runtime_url's `exit 1` killed only that subshell, `set -e` did
  # not see it, and the heredoc printed `AWS_A2A_ENDPOINT=` and returned 0.
  # deploy_master_aws.sh's `wire` then pushed the empty endpoint into the live
  # master, where the median absorbs the dead leg and the run exits 0 as well --
  # two layers of silent success over an absent cloud. Found on 2026-08-08 when
  # an expired AWS session produced exactly that.
  local url
  url="$(runtime_url)" || return 1
  if [[ -z "$url" || "$url" == "None" ]]; then
    echo "error: AWS_A2A_ENDPOINT did not resolve. Is the runtime deployed, and" >&2
    echo "       are your AWS credentials still valid? (aws sts get-caller-identity)" >&2
    return 1
  fi

  cat <<EOF
# Add to the master (AgentCore runtime) to reach this agent. Both ends are
# AgentCore runtimes in one account, so this is an IN-CLOUD HOP and is marked as
# one in the matrix -- it does not support the interop claim.
AWS_A2A_ENDPOINT=${url}
# No role assumption and no token exchange: the master signs with the
# credentials its own execution role already has.
AWS_A2A_AUTH=aws-sigv4-role
AWS_A2A_REGION=${REGION}
# bedrock-agentcore is already the default in coordinator/auth.py, and is what
# adds the mandatory X-Amzn-Bedrock-AgentCore-Runtime-Session-Id header --
# inside the signature, so it cannot be stripped or swapped in transit.
AWS_A2A_SIGNING_SERVICE=bedrock-agentcore
AWS_A2A_SESSION_ID=${SESSION_ID}
EOF
}

verify() {
  local url
  url="$(runtime_url)"

  echo "an authenticated leg is unproven without negative controls."
  echo

  echo "1. no signature -> expect 403 (SigV4 runtimes return ACCESS_DENIED,"
  echo "   with no WWW-Authenticate header -- unlike an OAuth-configured one,"
  echo "   which 401s and advertises its authorization server)"
  curl -s -o /dev/null -w '   %{http_code}\n' -X POST "$url"

  echo "2. unsigned agent-card fetch -> expect 403"
  curl -s -o /dev/null -w '   %{http_code}\n' "${url}.well-known/agent-card.json"

  echo "3. signed but with no session header -> AgentCore requires it on every"
  echo "   request; this is the control for that, not for auth"

  echo
  echo "The positive and negative controls for this leg run from the master,"
  echo "which is where the credentials are:"
  echo "   ./infra/deploy_master_aws.sh verify"
  echo
  echo "Note what this leg is now: master and agent are both AgentCore runtimes"
  echo "in one account, so it is an IN-CLOUD HOP. It is the control case for the"
  echo "signer, not evidence of interop."
}

# OPEN QUESTION 2, answered 2026-08-07. Kept as a verb because the answer is a
# claim about a live policy, and a claim about a live policy should be
# re-checkable rather than remembered.
scope_test() {
  echo "OPEN QUESTION 2: does a scoped policy permit the agent-card fetch, or"
  echo "is Resource:\"*\" required?"
  echo
  echo "ANSWERED: scoped works. Resource:\"*\" is NOT required. The predecessor"
  echo "diagnosis -- too-narrow resource -- was wrong; the card fetch is a"
  echo "separate ACTION, bedrock-agentcore:GetAgentCard, against the same two"
  echo "narrow resources. A missing action and a too-narrow resource both"
  echo "produce 403, which is why widening the resource looked like the fix."
  echo
  echo "The deployed policy, live. It moved with the master: the grant used to"
  echo "sit on the federated role this script created, and now sits on the"
  echo "master's own execution role, which is where an in-cloud caller's"
  echo "permissions belong."
  aws iam get-role-policy --role-name "${MASTER_ROLE_EXEC:-currency-master-agentcore-exec}" \
    --policy-name master-runtime \
    --query 'PolicyDocument.Statement[?contains(to_string(Action), `AgentRuntime`)].{Action:Action,Resource:Resource}' \
    --output json 2>/dev/null | sed 's/^/  /'
  echo
  echo "If Resource ever reads \"*\" here, this mesh is shipping a broad policy"
  echo "and that must be disclosed, not glossed."
  echo
  echo "Note: data-plane denials do not reach CloudTrail by default, so a 403"
  echo "from either cause is near-invisible until you enable them."
}

destroy() {
  local arn; arn="$(runtime_arn)"
  if [[ "$arn" != "None" && -n "$arn" ]]; then
    aws bedrock-agentcore-control delete-agent-runtime --region "$REGION" \
      --agent-runtime-id "$(basename "$arn")" >/dev/null 2>&1 || true
  fi
  aws iam delete-role-policy --role-name "$ROLE_EXEC" --policy-name agentcore-runtime 2>/dev/null || true
  aws iam delete-role --role-name "$ROLE_EXEC" 2>/dev/null || true
}

# The exec role without the image, so IAM can be reviewed before anything is
# built and so a propagation failure is not mistaken for a build failure.
roles_only() {
  ensure_exec_role
  echo "the caller's permission to invoke this runtime lives on the master's"
  echo "execution role, not here: ./infra/deploy_master_aws.sh wire"
}

# Every verb talks to AWS, so the preflight runs once here rather than being
# repeated (and forgotten) in each one.
ensure_aws_credentials || exit 1

case "${1:-deploy}" in
  deploy) deploy ;;
  roles) roles_only ;;
  env) env_block ;;
  verify) verify ;;
  scope-test) scope_test ;;
  url) runtime_url ;;
  destroy) destroy ;;
  *) echo "usage: $0 {deploy|roles|env|verify|scope-test|url|destroy}" >&2; exit 2 ;;
esac
