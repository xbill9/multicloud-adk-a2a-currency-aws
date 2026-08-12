#!/bin/bash
# The master, on Cloud Run as a **service**.
#
# The counterpart to deploy_master_aws.sh, and the reason it exists is a
# measurement rather than a preference: the retired Cloud Run master was a
# *job*, so every run paid container start (~1s) and the comparison against a
# warm AgentCore server was not like for like. A service is warm, which is what
# makes the two elapsed figures comparable.
#
# Three things about this topology, all of which cost time if assumed:
#
#   * The service must RUN AS currency-coordinator@<project>. That service
#     account's numeric unique ID is pinned in the AWS role's trust policy
#     (accounts.google.com:sub) and in the Entra federated credential's subject.
#     Deploy under the default compute SA and all three legs fail at the trust
#     check with messages that name no service account.
#
#   * --min-instances 1. Scale-to-zero reintroduces exactly the cold start this
#     deployment exists to remove, and the first request after an idle period
#     would be measured as though it were warm.
#
#   * The legs are wired GCP-rooted. The three agent scripts emit AWS-rooted
#     modes, because they were written for the AgentCore master, so their
#     endpoints are reused and their auth modes overridden -- last-wins, via the
#     same merge the AWS script uses.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"
REPO_NAME="${REPO_NAME:-currency-mesh}"
SERVICE="${MASTER_SERVICE:-currency-master}"
IMAGE="${IMAGE:-${REGION}-docker.pkg.dev/${PROJECT}/${REPO_NAME}/currency-master:latest}"

# The identity the federation is pinned to. Overridable, but changing it means
# re-pinning the AWS trust policy and the Entra FIC to the new numeric ID.
COORDINATOR_SA="${COORDINATOR_SA:-currency-coordinator@${PROJECT}.iam.gserviceaccount.com}"

# The AWS role this master assumes. Kept from the retired topology; its trust
# policy already names accounts.google.com and pins the SA's numeric ID.
AWS_ROLE_ARN="${AWS_ROLE_ARN:-}"
AWS_ROLE_NAME="${AWS_ROLE_NAME:-currency-aws-federated}"
AWS_REGION_="${AWS_REGION:-us-west-2}"

MESH_CLOUDS="${MESH_CLOUDS:-}"

service_url() {
  gcloud run services describe "$SERVICE" \
    --region "$REGION" --project "$PROJECT" \
    --format 'value(status.url)' 2>/dev/null
}

sa_unique_id() {
  gcloud iam service-accounts describe "$COORDINATOR_SA" \
    --project "$PROJECT" --format 'value(uniqueId)'
}

role_arn() {
  if [[ -n "$AWS_ROLE_ARN" ]]; then echo "$AWS_ROLE_ARN"; return; fi
  aws iam get-role --role-name "$AWS_ROLE_NAME" --query 'Role.Arn' --output text
}

# --------------------------------------------------------------------------
# Preflight: the federation is only as good as the identity it is pinned to
# --------------------------------------------------------------------------

check_pins() {
  local unique_id trust fic_subject app_id
  unique_id="$(sa_unique_id)"
  echo "coordinator SA : ${COORDINATOR_SA}"
  echo "numeric ID     : ${unique_id}"

  trust="$(aws iam get-role --role-name "$AWS_ROLE_NAME" \
    --query 'Role.AssumeRolePolicyDocument.Statement[0].Condition.StringEquals."accounts.google.com:sub"' \
    --output text 2>/dev/null || echo MISSING)"
  if [[ "$trust" == "$unique_id" ]]; then
    echo "aws trust      : OK (:sub pinned to this SA)"
  else
    echo "aws trust      : MISMATCH -- role pins ${trust}, SA is ${unique_id}" >&2
    echo "                 Google's token carries the numeric ID in sub; an email will never match." >&2
    return 1
  fi

  app_id="${AZURE_APP_ID:-$(az ad app list --display-name currency-mesh-master --query '[0].appId' -o tsv 2>/dev/null)}"
  fic_subject="$(az ad app federated-credential list --id "$app_id" \
    --query "[?name=='gcp-master'].subject | [0]" -o tsv 2>/dev/null || echo MISSING)"
  if [[ "$fic_subject" == "$unique_id" ]]; then
    echo "entra fic      : OK (subject pinned to this SA)"
  else
    echo "entra fic      : MISMATCH -- FIC subject ${fic_subject}, SA is ${unique_id}" >&2
    return 1
  fi
}

# --------------------------------------------------------------------------
# Build and deploy
# --------------------------------------------------------------------------

build() {
  gcloud artifacts repositories describe "$REPO_NAME" \
    --location "$REGION" --project "$PROJECT" >/dev/null 2>&1 || \
    gcloud artifacts repositories create "$REPO_NAME" \
      --repository-format=docker --location "$REGION" --project "$PROJECT" \
      --description="Three-cloud A2A currency mesh"

  # --tag would build the root Dockerfile, which is the local-mesh image. An
  # explicit config is the only way to name infra/Dockerfile.master.gcp.
  local cfg
  cfg="$(mktemp)"
  cat >"$cfg" <<YAML
steps:
  - name: gcr.io/cloud-builders/docker
    args: ["build", "-f", "infra/Dockerfile.master.gcp", "-t", "${IMAGE}", "."]
images: ["${IMAGE}"]
YAML
  gcloud builds submit "$REPO" --config "$cfg" --project "$PROJECT" \
    --gcs-source-staging-dir "gs://${PROJECT}_cloudbuild/source"
  rm -f "$cfg"
}

ensure_service_account() {
  gcloud iam service-accounts describe "$COORDINATOR_SA" --project "$PROJECT" >/dev/null 2>&1 || {
    echo "error: ${COORDINATOR_SA} does not exist." >&2
    echo "       It is the identity the AWS trust policy and the Entra FIC are pinned to;" >&2
    echo "       creating a new one means re-pinning both to its new numeric ID." >&2
    return 1
  }

  # The master calls the GCP agent, so it needs to be able to invoke it. The
  # agent script grants run.invoker to its own MASTER_SA; this is the same grant
  # for the identity this topology actually runs as.
  gcloud run services add-iam-policy-binding "${GCP_SERVICE:-currency-gcp}" \
    --region "$REGION" --project "$PROJECT" \
    --member "serviceAccount:${COORDINATOR_SA}" \
    --role roles/run.invoker --quiet >/dev/null 2>&1 || \
    echo "warning: could not grant run.invoker on the GCP agent; is it deployed?" >&2

  # The two grants that make `run` work at all -- see id_token() for why a user
  # credential alone cannot reach a private Cloud Run service.
  gcloud run services add-iam-policy-binding "$SERVICE" \
    --region "$REGION" --project "$PROJECT" \
    --member "serviceAccount:${COORDINATOR_SA}" \
    --role roles/run.invoker --quiet >/dev/null 2>&1 || true

  local caller
  caller="$(gcloud config get-value account 2>/dev/null)"
  if [[ -n "$caller" ]]; then
    gcloud iam service-accounts add-iam-policy-binding "$COORDINATOR_SA" \
      --project "$PROJECT" --member "user:${caller}" \
      --role roles/iam.serviceAccountTokenCreator --quiet >/dev/null 2>&1 || true
  fi
}

deploy() {
  build
  ensure_service_account

  gcloud run deploy "$SERVICE" \
    --image "$IMAGE" \
    --region "$REGION" --project "$PROJECT" \
    --service-account "$COORDINATOR_SA" \
    --no-allow-unauthenticated \
    --port 8080 \
    --min-instances 1 --max-instances 2 \
    --set-env-vars "CURRENCY_COORDINATOR_CLOUD=gcp,HOST=0.0.0.0" \
    --quiet

  echo "service: $(service_url)"
  echo
  echo "Now run: ./infra/deploy_master_gcp.sh wire"
}

# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------

# The agent scripts emit AWS-rooted auth. Their endpoints are what we want; the
# modes are not. Emitting the GCP-rooted block *after* theirs lets last-wins do
# the override, so there is one merge rule in this project rather than two.
peer_env() {
  echo "CURRENCY_COORDINATOR_CLOUD=gcp"
  echo "PUBLIC_URL=$(service_url)"
  echo "HOST=0.0.0.0"
  # No PORT: Cloud Run reserves it, injects it, and rejects any attempt to set
  # it ("The following reserved env names were provided: PORT"). The image's
  # ENV PORT=8080 still covers running the container anywhere else.
  if [[ -n "$MESH_CLOUDS" ]]; then echo "CURRENCY_MESH_CLOUDS=${MESH_CLOUDS}"; fi

  local script block
  for script in deploy_aws deploy_gcp deploy_azure; do
    if ! block="$("$REPO/infra/${script}.sh" env)"; then
      echo "error: ${script}.sh env failed; refusing to wire a partial mesh." >&2
      [[ "${ALLOW_PARTIAL_MESH:-}" == "1" ]] || return 1
      continue
    fi
    block="$(printf '%s\n' "$block" | grep -E '^[A-Z][A-Z0-9_]*=' || true)"
    if printf '%s\n' "$block" | grep -qE '^[A-Z][A-Z0-9_]*=$'; then
      echo "error: ${script}.sh env resolved these to nothing:" >&2
      printf '%s\n' "$block" | grep -E '^[A-Z][A-Z0-9_]*=$' | sed 's/^/         /' >&2
      [[ "${ALLOW_PARTIAL_MESH:-}" == "1" ]] || return 1
    fi
    if [[ -n "$block" ]]; then printf '%s\n' "$block"; fi
  done

  # The GCP-rooted overrides. Every one of these is keyless, which is the
  # difference this deployment exists to measure.
  local gcp_url
  gcp_url="$(gcloud run services describe "${GCP_SERVICE:-currency-gcp}" \
    --region "$REGION" --project "$PROJECT" --format 'value(status.url)' 2>/dev/null)"
  echo "GCP_A2A_AUTH=google-id-token"
  if [[ -n "$gcp_url" ]]; then echo "GCP_A2A_AUDIENCE=${gcp_url}"; fi

  echo "AWS_A2A_AUTH=aws-sigv4"
  echo "AWS_A2A_ROLE_ARN=$(role_arn)"
  echo "AWS_A2A_REGION=${AWS_REGION_}"

  echo "AZURE_A2A_AUTH=entra-fic"
  # The secret ARN the AgentCore topology needs is deliberately blanked: leaving
  # it set would work, and would quietly reintroduce the stored credential this
  # topology exists to do without.
  echo "AZURE_A2A_CLIENT_SECRET_ARN="
}

# Joined with '|', not ',', and handed to gcloud behind its `^|^` custom-delimiter
# prefix. Two values here would break a comma-delimited list, and both are easy
# to miss until a leg is silently half-configured:
#
#   * service account emails and image paths contain '@', so '@' is no safer
#   * CURRENCY_MESH_CLOUDS is itself a comma-separated list
#
# '|' appears in no URL, ARN, GUID or email this mesh produces.
env_with_overrides() {
  python3 -c '
import sys

merged = {}
for line in sys.stdin:
    line = line.strip()
    if not line or "=" not in line:
        continue
    key, value = line.split("=", 1)
    merged[key] = value          # last wins, which is what makes an override one
# An empty value is how a leg is *removed* rather than set to nothing: gcloud
# would happily set KEY= and the master would read it as configured-but-blank.
print("|".join(f"{k}={v}" for k, v in merged.items() if v != ""))
'
}

set_env() {
  gcloud run services update "$SERVICE" \
    --region "$REGION" --project "$PROJECT" \
    --set-env-vars "^|^${1}" \
    --quiet >/dev/null
}

wire() {
  local vars
  vars="$(peer_env | env_with_overrides)"
  [[ -z "$vars" ]] && { echo "nothing to wire" >&2; exit 1; }

  set_env "$vars"

  echo "master wired:"
  peer_env | sed 's/^/  /'
}

# --------------------------------------------------------------------------
# Invocation
# --------------------------------------------------------------------------

# Cloud Run validates an ID token whose audience is the service's own URL, and a
# *user* credential cannot mint one: gcloud hands back a token whose aud is its
# own OAuth client ID, and Cloud Run answers 401 with a body that says only
# "Your client does not have permission" -- naming neither the audience nor the
# identity. Impersonating the coordinator SA is what produces a token with the
# right aud, and it needs two grants that are easy to forget:
#
#   caller -> roles/iam.serviceAccountTokenCreator on the coordinator SA
#   SA     -> roles/run.invoker on this service
#
# Both are applied by `deploy`; this is the one place the difference shows up.
id_token() {
  gcloud auth print-identity-token \
    --impersonate-service-account="$COORDINATOR_SA" \
    --audiences="$(service_url)" 2>/dev/null
}

# The method is `SendMessage` and parts are {"text": ...} with role ROLE_USER --
# A2A v1.0's gRPC-style JSON naming. The REST-style spelling (`message/send`,
# {"kind": "text"}) is answered with -32601 Method not found, which reads like a
# broken server rather than a client using the other naming.
invoke() {
  local prompt="$1" url payload out rc=0
  url="$(service_url)"
  [[ -z "$url" ]] && { echo "error: ${SERVICE} is not deployed" >&2; return 1; }

  payload="$(python3 - "$prompt" <<'PY'
import json, sys, uuid
print(json.dumps({
    "jsonrpc": "2.0",
    "id": "1",
    "method": "SendMessage",
    "params": {"message": {
        "messageId": str(uuid.uuid4()),
        "role": "ROLE_USER",
        "parts": [{"text": sys.argv[1]}],
    }},
}))
PY
)"

  out="$(mktemp)"
  curl -sS -X POST "${url}/" \
    -H "Authorization: Bearer $(id_token)" \
    -H "Content-Type: application/json" \
    -d "$payload" -o "$out" || rc=$?

  if [[ "$rc" -ne 0 ]]; then
    echo "invoke failed (exit ${rc})" >&2
    rm -f "$out"
    return "$rc"
  fi

  # A FILE, not a pipe: `python3 - <<'PY'` reads the *program* from stdin, so a
  # piped reply is consumed by the interpreter and the reader reports an empty
  # response as though the master had sent one.
  python3 - "$out" <<'PY'
import json, sys

with open(sys.argv[1]) as handle:
    raw = handle.read()

try:
    envelope = json.loads(raw)
except ValueError:
    print(raw)
    raise SystemExit(1)

if "error" in envelope:
    print(f"JSON-RPC error: {envelope['error']}")
    raise SystemExit(1)

result = envelope.get("result", {})
parts = result.get("message", {}).get("parts", [])
text = "\n".join(part.get("text", "") for part in parts)
if not text:
    task = result.get("task", {})
    chunks = []
    for artifact in task.get("artifacts", []):
        chunks += [p.get("text", "") for p in artifact.get("parts", [])]
    for message in task.get("history", []):
        if message.get("role") == "ROLE_AGENT":
            chunks += [p.get("text", "") for p in message.get("parts", [])]
    text = "\n".join(c for c in chunks if c)

print(text or raw)
PY
  local prc=$?
  rm -f "$out"
  return "$prc"
}

# Print a mesh reply the way the CLI does: the consensus lines are the answer,
# the trailing envelope is what makes it checkable -- per-leg latency and the
# auth mode each leg actually used, which is the whole measurement here.
#
# Takes a FILE, not a pipe, for the reason invoke() states.
render_mesh() {
  python3 - "$1" <<'PY'
import json, sys

with open(sys.argv[1]) as handle:
    text = handle.read()
print(text)

envelope = None
for line in text.splitlines():
    line = line.strip()
    if line.startswith('{"mesh_run"'):
        try:
            envelope = json.loads(line)["mesh_run"]
        except ValueError:
            pass

if envelope is None:
    print("\nno mesh_run envelope in the reply", file=sys.stderr)
    raise SystemExit(1)

print()
modes = ", ".join(f"{k}={v}" for k, v in sorted(envelope.get("auth_modes", {}).items()))
print(f"participants: {', '.join(envelope.get('participants', []))}")
print(f"auth        : {modes}")
for result in envelope.get("results", []):
    quotes = result.get("quotes", [])
    verdict = {True: "agreed", False: "DISAGREED", None: "unverified"}[result.get("agreed")]
    if result.get("consensus_amount") is None:
        print(f"{result['target_currency']}: no cloud answered")
        continue
    print(f"{result['target_currency']}: {result['consensus_amount']} "
          f"[{len(quotes)} clouds, {verdict}]")
    for quote in quotes:
        print(f"    {quote['source']:<8} {quote['converted_amount']:>14} "
              f"({quote['latency_ms']:.0f}ms)")
for name, failure in envelope.get("failures", {}).items():
    print(f"  {name} failure: {failure}")
print(f"elapsed {envelope['elapsed_ms']:.0f}ms")

raise SystemExit(0 if any(r.get("consensus_amount") is not None
                          for r in envelope.get("results", [])) else 1)
PY
}

# The prompt is a strict template -- agents/common.py matches it with a regex
# rather than a model, so an approximation is declined rather than parsed.
run() {
  local targets="${1:-EUR, JPY}" reply rc=0
  reply="$(mktemp)"
  invoke "Convert 100 USD to the following currencies: ${targets}. Reply with JSON." \
    > "$reply" || rc=$?
  [[ "$rc" -eq 0 ]] && { render_mesh "$reply" || rc=$?; }
  rm -f "$reply"
  return "$rc"
}

matrix() {
  invoke "matrix"
}

# --------------------------------------------------------------------------
# Negative controls
#
# Cloud Run services take an env override per revision, not per execution the
# way a job did, so each probe deploys a revision and the wiring is restored
# afterwards. That is a real loss against the job topology and it is why the
# comparison reports the control mechanism as a difference rather than a tie.
# --------------------------------------------------------------------------

# Each probe writes the *whole* wired environment plus its one override, rather
# than updating a key in place. Two reasons, and the second is the one that
# ruins a control run:
#
#   * --set-env-vars replaces the entire set, so updating a single key with it
#     would wipe every leg's configuration and leave a master that reaches
#     nothing -- which reads as a denial and is not one
#   * --update-env-vars would merge, but then each probe's override survives
#     into the next, and by the third probe two legs are disabled rather than
#     one. A control that does not isolate one leg is not a control.
probe() {
  local label="$1" clouds="$2" override="$3" expect="$4" out vars
  vars="$(
    {
      peer_env
      echo "CURRENCY_MESH_CLOUDS=${clouds}"
      # `if`, not `[[ ... ]] &&`: this is the last statement in the group, so a
      # false test leaves the group at status 1, which under `set -o pipefail`
      # fails the pipeline, fails the assignment, and `set -e` kills the script
      # with no message. It would fire on exactly the probes with no override --
      # the three positive controls. Same trap as deploy_master_aws.sh.
      if [[ -n "$override" ]]; then echo "$override"; fi
    } | env_with_overrides
  )"
  set_env "$vars"

  out="$(invoke "Convert 100 USD to EUR" 2>&1 || true)"
  if grep -qi "$expect" <<<"$out"; then
    echo "  ${label}: OK (${expect})"
  else
    echo "  ${label}: UNEXPECTED" >&2
    printf '%s\n' "$out" | head -5 | sed 's/^/    /' >&2
  fi
}

verify() {
  local url
  url="$(service_url)"

  echo "unauthenticated:"
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' -X POST "${url}/" -d '{}')"
  [[ "$code" == "403" || "$code" == "401" ]] && echo "  invoke: OK (${code})" \
    || echo "  invoke: UNEXPECTED (${code})" >&2

  echo "positive controls (each leg alone, as deployed):"
  for cloud in gcp aws azure; do
    probe "$cloud answers" "$cloud" "" "target_currency"
  done

  echo "negative controls (each leg alone, credential removed):"
  probe "gcp denied"   "gcp"   "GCP_A2A_AUTH=none"   "error"
  probe "aws denied"   "aws"   "AWS_A2A_AUTH=none"   "error"
  probe "azure denied" "azure" "AZURE_A2A_AUTH=none" "error"

  echo "restoring the wiring"
  wire >/dev/null
  echo "  done"
}

destroy() {
  gcloud run services delete "$SERVICE" --region "$REGION" --project "$PROJECT" --quiet || true
}

usage() {
  cat <<'TXT'
usage: deploy_master_gcp.sh <command>

  check     verify the AWS trust policy and Entra FIC still pin this SA
  deploy    build the amd64 image and deploy the master as a Cloud Run service
  wire      fold the three legs in, GCP-rooted and keyless
  run       one three-cloud consensus run
  matrix    the 3x3 interop matrix, from the master
  verify    the negative controls
  url       print the service URL
  destroy   delete the service
TXT
}

case "${1:-usage}" in
  check) check_pins ;;
  deploy) deploy ;;
  wire) wire ;;
  run) shift; run "${1:-EUR, JPY}" ;;
  matrix) matrix ;;
  verify) verify ;;
  url) service_url ;;
  destroy) destroy ;;
  *) usage ;;
esac
