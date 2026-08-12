#!/usr/bin/env bash
# Deploy the GCP leg: the ADK agent as an authenticated Cloud Run service, plus
# the workload identity federation that lets the AWS-hosted master reach it
# without a stored secret.
#
#   ./infra/deploy_gcp.sh deploy   # build + deploy the ADK service
#   ./infra/deploy_gcp.sh wif      # pool, AWS provider, SA, and the two grants
#   ./infra/deploy_gcp.sh env      # env vars to add to the master
#   ./infra/deploy_gcp.sh verify   # what an unauthenticated caller gets
#   ./infra/deploy_gcp.sh url
#   ./infra/deploy_gcp.sh destroy
#
# There is no coordinator job here any more. The master runs on Bedrock
# AgentCore -- see infra/deploy_master_aws.sh -- so this cloud is a callee only,
# and the interesting half of this script is `wif`: the pool that lets an AWS
# role become a Google identity.
#
# The mechanism is worth understanding before reading it. Google's Workload
# Identity Federation accepts an AWS-shaped subject token: a SigV4-signed
# GetCallerIdentity request, serialised and handed over *unsent*, which Google
# replays against AWS STS to learn who signed it. No JWT is minted anywhere, so
# it works from a runtime that cannot mint OIDC at all -- which an AgentCore
# execution role cannot.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"
REPO_NAME="${REPO_NAME:-currency-mesh}"
SERVICE="${SERVICE:-currency-gcp}"
POOL="${WIF_POOL:-currency-mesh}"
PROVIDER="${WIF_PROVIDER:-aws-master}"
#: The identity the federated AWS principal impersonates. It holds
#: roles/run.invoker; the AWS role holds nothing on this project directly.
MASTER_SA="${MASTER_SA:-currency-master@${PROJECT}.iam.gserviceaccount.com}"

# `direct` stays the default, for the reason in docs/DEPLOYMENT_PLAN.md: the
# matrix is a protocol instrument, and a model in the path makes a red cell
# ambiguous. MODEL_MODE=llm deploys the brain. ADK does not use ADC for
# Gemini, hence GOOGLE_GENAI_USE_VERTEXAI and the project/location pair --
# without them it asks for an API key and fails inside a task body with
# HTTP 200.
MODEL_MODE="${MODEL_MODE:-direct}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO_NAME}/currency-mesh:latest"

service_url() {
  gcloud run services describe "$SERVICE" \
    --region "$REGION" --project "$PROJECT" --format='value(status.url)'
}

project_number() {
  gcloud projects describe "$PROJECT" --format='value(projectNumber)'
}

build() {
  gcloud artifacts repositories describe "$REPO_NAME" \
    --location "$REGION" --project "$PROJECT" >/dev/null 2>&1 || \
    gcloud artifacts repositories create "$REPO_NAME" \
      --repository-format=docker --location "$REGION" --project "$PROJECT" \
      --description="Three-cloud A2A currency mesh"

  gcloud builds submit "$REPO" \
    --tag "$IMAGE" --project "$PROJECT" \
    --gcs-source-staging-dir "gs://${PROJECT}_cloudbuild/source"
}

deploy() {
  build

  # --no-allow-unauthenticated is the point of the exercise: the service
  # rejects anything without a valid Google ID token whose audience is this
  # service's own URL.
  gcloud run deploy "$SERVICE" \
    --image "$IMAGE" \
    --region "$REGION" --project "$PROJECT" \
    --no-allow-unauthenticated \
    --port 8080 \
    --set-env-vars "CURRENCY_MODEL_MODE=${MODEL_MODE},HOST=0.0.0.0,GOOGLE_GENAI_USE_VERTEXAI=true,GOOGLE_CLOUD_PROJECT=${PROJECT},GOOGLE_CLOUD_LOCATION=${REGION}" \
    --min-instances 0 --max-instances 2 \
    --quiet

  echo "service: $(service_url)"
  echo
  echo "Now run: ./infra/deploy_gcp.sh wif"
}

ensure_service_account() {
  gcloud iam service-accounts describe "$MASTER_SA" --project "$PROJECT" >/dev/null 2>&1 || \
    gcloud iam service-accounts create "${MASTER_SA%%@*}" --project "$PROJECT" \
      --display-name "Impersonated by the AgentCore-hosted currency mesh master"

  # Audience alone is not authorization -- it is caller-chosen. This IAM
  # binding is what actually authorizes the call; the token only proves who is
  # asking.
  gcloud run services add-iam-policy-binding "$SERVICE" \
    --region "$REGION" --project "$PROJECT" \
    --member "serviceAccount:${MASTER_SA}" \
    --role roles/run.invoker --quiet >/dev/null
  echo "granted roles/run.invoker to ${MASTER_SA}"
}

# The AWS role the pool trusts, read back from the script that created it
# rather than restated here. A role ARN copied into two files is a role ARN
# that will eventually disagree with itself.
master_role_arn() {
  local arn
  arn="$("$REPO/infra/deploy_master_aws.sh" role-arn 2>/dev/null || true)"
  if [[ -z "$arn" || "$arn" == "None" ]]; then
    echo "cannot read the master's execution role ARN." >&2
    echo "  run ./infra/deploy_master_aws.sh deploy first -- the pool has to name" >&2
    echo "  the role it trusts, and an unconditioned pool trusts the whole account." >&2
    exit 1
  fi
  echo "$arn"
}

wif() {
  local number account role_arn assumed_role principal
  number="$(project_number)"
  role_arn="$(master_role_arn)"
  account="$(printf '%s' "$role_arn" | cut -d: -f5)"
  # The pool sees the *assumed-role* ARN, not the role ARN that was granted:
  # arn:aws:iam::123:role/foo is presented as arn:aws:sts::123:assumed-role/foo.
  # Writing the granted ARN into the condition is the single most common way
  # this fails, and it denies with permission_denied rather than with anything
  # that names the string mismatch.
  assumed_role="arn:aws:sts::${account}:assumed-role/$(basename "${role_arn}")"

  gcloud iam workload-identity-pools describe "$POOL" \
    --location=global --project "$PROJECT" >/dev/null 2>&1 || \
    gcloud iam workload-identity-pools create "$POOL" \
      --location=global --project "$PROJECT" \
      --display-name="Currency mesh" \
      --description="Trusts the AgentCore-hosted master's execution role"

  # attribute-condition pins the principal to one role. Without it the provider
  # trusts *every* identity in the AWS account, which is the same class of
  # mistake as an audience-only condition on the AWS side: it proves the token
  # came from the right issuer and says nothing about who.
  if gcloud iam workload-identity-pools providers describe "$PROVIDER" \
       --workload-identity-pool="$POOL" --location=global --project "$PROJECT" >/dev/null 2>&1; then
    gcloud iam workload-identity-pools providers update-aws "$PROVIDER" \
      --workload-identity-pool="$POOL" --location=global --project "$PROJECT" \
      --attribute-condition="attribute.aws_role == '${assumed_role}'" --quiet
  else
    gcloud iam workload-identity-pools providers create-aws "$PROVIDER" \
      --workload-identity-pool="$POOL" --location=global --project "$PROJECT" \
      --account-id="$account" \
      --attribute-condition="attribute.aws_role == '${assumed_role}'" \
      --display-name="AgentCore master"
  fi

  ensure_service_account

  # The federated principal impersonates the service account, and this grant is
  # what permits it. It is easy to forget, and forgetting it denies with a 403
  # that names the *service account* rather than the pool -- so it reads as a
  # federation failure when the federation already succeeded.
  principal="principalSet://iam.googleapis.com/projects/${number}/locations/global/workloadIdentityPools/${POOL}/attribute.aws_role/${assumed_role}"
  gcloud iam service-accounts add-iam-policy-binding "$MASTER_SA" \
    --project "$PROJECT" \
    --role=roles/iam.serviceAccountTokenCreator \
    --member="$principal" --quiet >/dev/null
  echo "granted roles/iam.serviceAccountTokenCreator on ${MASTER_SA} to:"
  echo "  ${principal}"
  echo
  echo "Now run: ./infra/deploy_master_aws.sh wire"
}

pool_provider() {
  echo "//iam.googleapis.com/projects/$(project_number)/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}"
}

env_block() {
  # Resolve and validate before emitting anything. An empty *value* is the
  # dangerous case: a leg wired to `GCP_A2A_ENDPOINT=` leaves the master
  # degrading over a dead cloud and answering 2/3 without comment.
  local url provider
  url="$(service_url)" || return 1
  provider="$(pool_provider)" || return 1
  if [[ -z "$url" || -z "$provider" ]]; then
    echo "error: the GCP leg did not resolve. Is ${SERVICE} deployed, and has" >&2
    echo "       ./infra/deploy_gcp.sh wif been run?" >&2
    return 1
  fi

  cat <<EOF
# Add to the master (AgentCore runtime) to reach this agent:
GCP_A2A_ENDPOINT=${url}
GCP_A2A_AUTH=gcp-wif-aws
GCP_A2A_POOL_PROVIDER=${provider}
GCP_A2A_SERVICE_ACCOUNT=${MASTER_SA}
# Cloud Run validates an ID token whose audience is its own service URL. The
# STS exchange yields an *access* token, which is why the master impersonates
# the service account above for an ID token rather than presenting what it got.
GCP_A2A_AUDIENCE=${url}
EOF
}

verify() {
  local url; url="$(service_url)"
  echo "unauthenticated, from here -- no Google token at all"
  echo "   /health    -> $(curl -s -o /dev/null -w '%{http_code}' -m 25 "${url}/health")   (expect 403)"
  echo "   agent card -> $(curl -s -o /dev/null -w '%{http_code}' -m 25 "${url}/.well-known/agent-card.json")   (expect 403)"
  echo
  echo "The positive and negative controls for this leg run from the master,"
  echo "which is the only principal the pool trusts:"
  echo "   ./infra/deploy_master_aws.sh verify"
}

destroy() {
  gcloud run services delete "$SERVICE" --region "$REGION" --project "$PROJECT" --quiet || true
  gcloud iam workload-identity-pools providers delete "$PROVIDER" \
    --workload-identity-pool="$POOL" --location=global --project "$PROJECT" --quiet || true
  gcloud iam workload-identity-pools delete "$POOL" \
    --location=global --project "$PROJECT" --quiet || true
  gcloud iam service-accounts delete "$MASTER_SA" --project "$PROJECT" --quiet || true
}

case "${1:-deploy}" in
  build) build ;;
  deploy) deploy ;;
  wif) wif ;;
  env) env_block ;;
  verify) verify ;;
  url) service_url ;;
  destroy) destroy ;;
  *) echo "usage: $0 {build|deploy|wif|env|verify|url|destroy}" >&2; exit 2 ;;
esac
