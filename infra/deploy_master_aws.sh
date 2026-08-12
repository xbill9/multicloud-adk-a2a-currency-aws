#!/usr/bin/env bash
# Deploy the master: the three-cloud coordinator, itself an A2A agent on
# Bedrock AgentCore Runtime.
#
#   ./infra/deploy_master_aws.sh deploy   # ECR, ARM64 image, runtime, exec role
#   ./infra/deploy_master_aws.sh wire     # fold all three legs into the runtime
#   ./infra/deploy_master_aws.sh run      # one three-cloud consensus, from the cloud
#   ./infra/deploy_master_aws.sh matrix   # the 3x3 against every hosted server
#   ./infra/deploy_master_aws.sh verify   # negative controls -- run these
#   ./infra/deploy_master_aws.sh url
#   ./infra/deploy_master_aws.sh destroy
#
# The master runs on an agent runtime rather than on Lambda or ECS because the
# mesh is a mesh of agents: making the one that coordinates them a plain
# function would concede the premise. It is reached over A2A, exactly like the
# three peers, and answers the same prompt template in the same wire format.
#
# What moving it here costs is written down rather than glossed. A Cloud-Run
# rooted master reached all three clouds keylessly, because Cloud Run mints
# workload OIDC for an arbitrary audience. An AgentCore execution role is not an
# OIDC issuer, so:
#
#   AWS -> AWS    SigV4, this runtime's own role        keyless, in-cloud hop
#   AWS -> GCP    GetCallerIdentity -> GCP STS -> impersonate      keyless
#   AWS -> Azure  Entra client secret                   NOT keyless
#
# The Azure secret lives in Secrets Manager and is read with the same role, so
# it is not a plaintext value in the runtime's configuration. That reduces the
# blast radius; it does not restore the claim.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGION="${AWS_REGION:-us-west-2}"
RUNTIME="${MASTER_RUNTIME:-currency_master}"
PEER_RUNTIME="${RUNTIME_AWS:-currency_aws}"
ECR_REPO="${ECR_REPO:-currency-mesh}"
ROLE_EXEC="${MASTER_ROLE_EXEC:-currency-master-agentcore-exec}"
DOCKER="${DOCKER:-docker}"

# The mesh the master fans out to. Left unset in the runtime environment means
# all three; `verify` overrides it per probe.
MESH_CLOUDS="${MESH_CLOUDS:-}"

# Session IDs must be >= 33 characters (AgentCore rejects shorter ones) and are
# pinned rather than minted per call: each new session gets its own microVM, and
# a cold one is worth several seconds. That cost was once measured, attributed
# to the client stack, written up as an anomaly, and then retracted.
#
# **The runtime version is in the ID, and that is not cosmetic.** A session is
# sticky to the container it started in, so a pinned ID keeps reaching the
# microVM that session began on -- including across a deploy. Measured
# 2026-08-12: after pushing a new image and updating the runtime, `run` returned
# an error string that had been deleted from the source, while a *different* log
# stream showed the new code starting cleanly. Same runtime, two versions
# serving at once, and the pinned session routed to the old one. Deriving the ID
# from the version means a deploy rotates it automatically; forgetting to would
# mean testing the previous build and believing it was this one.
session_id() {
  if [[ -n "${MASTER_SESSION_ID:-}" ]]; then
    echo "$MASTER_SESSION_ID"
    return
  fi
  local arn version
  arn="$(runtime_arn)"
  version="$(aws bedrock-agentcore-control get-agent-runtime --region "$REGION" \
    --agent-runtime-id "$(basename "$arn")" --query 'agentRuntimeVersion' \
    --output text 2>/dev/null || echo 0)"
  # 32 characters before the suffix, so any version keeps this over the minimum.
  echo "currency-master-operator-session-v${version}"
}

CREDS_FILE="${CREDS_FILE:-${REPO}/.aws_creds}"

account_id() { aws sts get-caller-identity --query Account --output text; }

# See the long note in deploy_aws.sh: this captures and re-injects credentials
# that already exist. It cannot refresh a dead session and cannot rescue a shell
# whose AWS_* variables are wrong, because environment wins the AWS credential
# chain.
ensure_aws_credentials() {
  aws sts get-caller-identity >/dev/null 2>&1 && return 0

  echo "aws credentials are not usable in this shell; trying save-aws-creds.sh" >&2
  if [[ -x "$REPO/save-aws-creds.sh" ]] \
     && (cd "$REPO" && ./save-aws-creds.sh "$CREDS_FILE" >/dev/null 2>&1); then
    # shellcheck disable=SC1090
    set -a; . "$CREDS_FILE"; set +a
    aws sts get-caller-identity >/dev/null 2>&1 && { echo "recovered from ${CREDS_FILE}" >&2; return 0; }
  fi
  if [[ -r "$CREDS_FILE" ]]; then
    # shellcheck disable=SC1090
    set -a; . "$CREDS_FILE"; set +a
    aws sts get-caller-identity >/dev/null 2>&1 && {
      echo "recovered from the existing ${CREDS_FILE}" >&2; return 0; }
  fi

  echo "error: no usable AWS credentials, and save-aws-creds.sh cannot make any." >&2
  echo "       the session itself is gone -- re-authenticate, then re-run:" >&2
  echo "         aws login          (or: aws sso login)" >&2
  return 1
}

# Retried for the reason documented at length in deploy_aws.sh:
# bedrock-agentcore-control intermittently fails this call with a
# CreateOAuth2Token ValidationException on a session that is demonstrably valid,
# and the windows are minutes rather than seconds. Retry only when the *call*
# fails -- a call that succeeds and reports no such runtime is a fact.
runtime_arn_of() {
  local name="$1" attempt arn rc delay=3
  for attempt in 1 2 3 4 5 6 7; do
    rc=0
    arn="$(aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" \
      --query "agentRuntimes[?agentRuntimeName=='${name}'].agentRuntimeArn | [0]" \
      --output text 2>/dev/null)" || rc=$?
    if [[ "$rc" -eq 0 ]]; then
      [[ "$attempt" -gt 1 ]] && echo "note: ${name} ARN resolved on attempt ${attempt}" >&2
      echo "${arn:-None}"
      return 0
    fi
    [[ "$attempt" -lt 7 ]] && { sleep "$delay"; [[ "$delay" -lt 30 ]] && delay=$((delay * 2)); }
  done
  return 1
}

runtime_arn() { runtime_arn_of "$RUNTIME"; }

runtime_url() {
  local arn escaped
  arn="$(runtime_arn)" || arn=""
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
  image="${account}.dkr.ecr.${REGION}.amazonaws.com/${ECR_REPO}:master"

  # Everything to stderr except the final echo: the caller does
  # image="$(build_and_push)", so one stray stdout line -- docker login's
  # "Login Succeeded", a push layer -- is concatenated into the image URI and
  # then passed as a containerUri, failing in a way that names neither.
  {
    aws ecr describe-repositories --repository-names "$ECR_REPO" --region "$REGION" \
      >/dev/null 2>&1 || \
      aws ecr create-repository --repository-name "$ECR_REPO" --region "$REGION" >/dev/null

    aws ecr get-login-password --region "$REGION" \
      | $DOCKER login --username AWS --password-stdin \
          "${account}.dkr.ecr.${REGION}.amazonaws.com"

    # ARM64 is required by AgentCore, not preferred. On an x86 host this needs
    # buildx plus qemu; a silently-amd64 image is accepted by ECR and then fails
    # at runtime with an exec-format error naming neither the architecture nor
    # the image.
    $DOCKER buildx build --platform linux/arm64 --load \
      -f "$REPO/infra/Dockerfile.master" -t "$image" "$REPO"
    $DOCKER push "$image"
  } >&2

  echo "$image"
}

# The execution role is the master's identity on every leg, which is what makes
# it worth reading closely: it invokes the AWS peer, it is the principal GCP's
# workload identity pool trusts, and it reads the one secret this topology could
# not avoid.
ensure_exec_role() {
  local account created=0 peer_arn secret_arn
  account="$(account_id)"
  peer_arn="$(runtime_arn_of "$PEER_RUNTIME")" || peer_arn="None"
  secret_arn="$("$REPO/infra/deploy_azure.sh" secret-arn 2>/dev/null || true)"

  if ! aws iam get-role --role-name "$ROLE_EXEC" >/dev/null 2>&1; then
    created=1
    aws iam create-role --role-name "$ROLE_EXEC" \
      --description "Execution role for the AgentCore-hosted currency mesh master" \
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

  # Re-applied on every run rather than only at creation. put-role-policy
  # overwrites, so this is idempotent -- and the alternative, returning early
  # when the role exists, is how a later addition silently never reaches an
  # already-deployed role.
  local statements
  statements="
    {
      \"Effect\": \"Allow\",
      \"Action\": [\"ecr:GetAuthorizationToken\", \"ecr:BatchGetImage\", \"ecr:GetDownloadUrlForLayer\"],
      \"Resource\": \"*\"
    },
    {
      \"Effect\": \"Allow\",
      \"Action\": [\"logs:CreateLogGroup\", \"logs:CreateLogStream\", \"logs:PutLogEvents\"],
      \"Resource\": \"arn:aws:logs:${REGION}:${account}:*\"
    }"

  # Both actions, scoped to the peer runtime and its children. GetAgentCard is
  # separate from InvokeAgentRuntime and is the whole of open question 2: the
  # predecessor series blamed a too-narrow *resource* for a card-fetch 403 that
  # was in fact a missing *action*, and shipped Resource:"*" for a year.
  if [[ "$peer_arn" != "None" && -n "$peer_arn" ]]; then
    statements="${statements},
    {
      \"Effect\": \"Allow\",
      \"Action\": [\"bedrock-agentcore:InvokeAgentRuntime\", \"bedrock-agentcore:GetAgentCard\"],
      \"Resource\": [\"${peer_arn}\", \"${peer_arn}/*\"]
    }"
  else
    echo "note: ${PEER_RUNTIME} is not deployed yet, so the master cannot be granted" >&2
    echo "      InvokeAgentRuntime on it. Deploy the AWS agent, then re-run this." >&2
  fi

  if [[ -n "$secret_arn" && "$secret_arn" != "None" ]]; then
    statements="${statements},
    {
      \"Effect\": \"Allow\",
      \"Action\": [\"secretsmanager:GetSecretValue\"],
      \"Resource\": \"${secret_arn}\"
    }"
  fi

  aws iam put-role-policy --role-name "$ROLE_EXEC" \
    --policy-name master-runtime \
    --policy-document "{\"Version\": \"2012-10-17\", \"Statement\": [${statements}]}"

  if [[ "$created" == 1 ]]; then
    echo "waiting for ${ROLE_EXEC} to propagate"
    sleep 15
  fi
}

# Every update passes the *whole* configuration, because update-agent-runtime
# replaces rather than merges. Dropping protocolConfiguration on a partial
# update once left a runtime READY, passing its health check, and unable to
# serve a single A2A request. One helper, so that cannot happen twice.
update_runtime() {
  local arn="$1" image="$2" account="$3"; shift 3
  aws bedrock-agentcore-control update-agent-runtime --region "$REGION" \
    --agent-runtime-id "$(basename "$arn")" \
    --agent-runtime-artifact "containerConfiguration={containerUri=${image}}" \
    --role-arn "arn:aws:iam::${account}:role/${ROLE_EXEC}" \
    --network-configuration 'networkMode=PUBLIC' \
    --protocol-configuration 'serverProtocol=A2A' \
    "$@" >/dev/null
}

current_image() {
  local arn; arn="$(runtime_arn)"
  aws bedrock-agentcore-control get-agent-runtime --region "$REGION" \
    --agent-runtime-id "$(basename "$arn")" \
    --query 'agentRuntimeArtifact.containerConfiguration.containerUri' --output text
}

current_env() {
  local arn; arn="$(runtime_arn)"
  aws bedrock-agentcore-control get-agent-runtime --region "$REGION" \
    --agent-runtime-id "$(basename "$arn")" \
    --query 'environmentVariables' --output json
}

deploy() {
  local image account arn url
  image="$(build_and_push)"
  account="$(account_id)"
  ensure_exec_role

  # `|| arn=""` is load-bearing: runtime_arn returns non-zero when no runtime
  # exists, which is exactly the state of a first deploy, and an unguarded
  # assignment under `set -e` exits here silently before create is ever reached.
  arn="$(runtime_arn)" || arn=""
  if [[ "$arn" == "None" || -z "$arn" ]]; then
    aws bedrock-agentcore-control create-agent-runtime --region "$REGION" \
      --agent-runtime-name "$RUNTIME" \
      --agent-runtime-artifact "containerConfiguration={containerUri=${image}}" \
      --role-arn "arn:aws:iam::${account}:role/${ROLE_EXEC}" \
      --network-configuration 'networkMode=PUBLIC' \
      --protocol-configuration 'serverProtocol=A2A' >/dev/null
    # --authorizer-configuration is OMITTED, which is what selects SigV4. It is
    # a tagged union whose only member is customJWTAuthorizer, so an empty '{}'
    # is rejected with an error that reads as though the field were required
    # when in fact it must be absent.
  else
    update_runtime "$arn" "$image" "$account"
  fi

  arn="$(runtime_arn)" || {
    echo "error: runtime was created but its ARN did not resolve." >&2
    echo "       re-run deploy; the create is idempotent from here." >&2
    return 1
  }
  url="$(runtime_url)"

  # Two-phase by necessity: the card must advertise the invocations URL, which
  # is derived from an ARN that does not exist until the runtime does.
  update_runtime "$arn" "$image" "$account" \
    --environment-variables "PUBLIC_URL=${url},HOST=0.0.0.0,PORT=9000,CURRENCY_COORDINATOR_CLOUD=aws"

  echo
  echo "master arn : $arn"
  echo "master url : $url"
  echo
  echo "Now run: ./infra/deploy_master_aws.sh wire"
}

# The three-cloud env, assembled from the per-cloud scripts rather than restated
# here. Each cloud's identifiers live in exactly one place -- the script that
# created them -- so a redeployed runtime (whose URL contains its own ARN) or a
# recreated app registration cannot leave a stale copy behind.
peer_env() {
  echo "CURRENCY_COORDINATOR_CLOUD=aws"
  echo "PUBLIC_URL=$(runtime_url)"
  echo "HOST=0.0.0.0"
  echo "PORT=9000"
  if [[ -n "$MESH_CLOUDS" ]]; then echo "CURRENCY_MESH_CLOUDS=${MESH_CLOUDS}"; fi
  # The signer for the GCP leg falls back to this when GCP_A2A_REGION is unset.
  # AgentCore sets it too, but stating it here means one script owns the value.
  echo "AWS_REGION=${REGION}"

  # An empty *value* is the dangerous case, not empty output. A sibling that
  # cannot resolve its endpoint emitting `GCP_A2A_ENDPOINT=` used to sail
  # through a guard that only checked some assignment came back; the master then
  # degrades over the dead leg and answers 2/3 without comment.
  #
  # stderr is deliberately not swallowed: the sibling script explains *why* it
  # could not resolve, and that message is the entire diagnosis.
  local script block
  for script in deploy_aws deploy_gcp deploy_azure; do
    if ! block="$("$REPO/infra/${script}.sh" env)"; then
      echo "error: ${script}.sh env failed; refusing to wire a partial mesh." >&2
      echo "       set ALLOW_PARTIAL_MESH=1 to wire the legs that do resolve." >&2
      [[ "${ALLOW_PARTIAL_MESH:-}" == "1" ]] || return 1
      continue
    fi
    block="$(printf '%s\n' "$block" | grep -E '^[A-Z][A-Z0-9_]*=' || true)"
    if printf '%s\n' "$block" | grep -qE '^[A-Z][A-Z0-9_]*=$'; then
      echo "error: ${script}.sh env resolved these to nothing:" >&2
      printf '%s\n' "$block" | grep -E '^[A-Z][A-Z0-9_]*=$' | sed 's/^/         /' >&2
      echo "       refusing to wire a leg the master cannot reach." >&2
      [[ "${ALLOW_PARTIAL_MESH:-}" == "1" ]] || return 1
    fi
    # `if`, not a trailing &&: this is the last statement in the loop, so a
    # false test would make peer_env itself return 1 and silently abort every
    # caller under `set -e`. peer_env must be able to fail, but only when it
    # means to.
    if [[ -n "$block" ]]; then printf '%s\n' "$block"; fi
  done
}

# Fold KEY=VALUE lines into the one comma-separated string the CLI wants, with
# **last occurrence winning per key**.
#
# `--environment-variables` rejects a repeated key outright ("Second instance of
# key ... encountered"), so a control that appends `AWS_A2A_AUTH=none` to a wired
# environment that already sets it does not override anything -- it fails to
# deploy. That killed every negative control while all three positive ones
# passed, which is the worst possible shape for a failure in a control harness:
# the probes that must fail were the only ones not running.
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
print(",".join(f"{k}={v}" for k, v in merged.items()))
'
}

wire() {
  local vars arn image account
  vars="$(peer_env | env_with_overrides)"
  [[ -z "$vars" ]] && { echo "nothing to wire" >&2; exit 1; }

  arn="$(runtime_arn)"
  image="$(current_image)"
  account="$(account_id)"
  # The exec role is refreshed here too: the peer runtime and the Azure secret
  # may not have existed when the master was first deployed, and a grant that
  # only happens at creation is a grant that silently never arrives.
  ensure_exec_role
  update_runtime "$arn" "$image" "$account" --environment-variables "$vars"

  echo "master wired:"
  peer_env | sed 's/^/  /'
}

# Wait for the runtime to be serving the configuration we just wrote. Without
# this, an invoke immediately after an update reaches the previous version and
# reports a result for a configuration that is no longer deployed -- which is
# indistinguishable from the control having failed.
wait_ready() {
  local arn status attempt
  arn="$(runtime_arn)"
  for attempt in $(seq 1 60); do
    status="$(aws bedrock-agentcore-control get-agent-runtime --region "$REGION" \
      --agent-runtime-id "$(basename "$arn")" --query 'status' --output text 2>/dev/null || echo UNKNOWN)"
    [[ "$status" == "READY" ]] && return 0
    sleep 5
  done
  echo "warning: ${RUNTIME} is ${status}, not READY, after 5 minutes" >&2
  return 1
}

# One A2A JSON-RPC call into the master. The method name is `SendMessage`, not
# `message/send`: a2a-sdk 1.x routes JSON-RPC by the gRPC service method name,
# and the older spelling returns -32601 Method not found.
invoke() {
  local prompt="$1" arn payload out rc=0
  arn="$(runtime_arn)"
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
  aws bedrock-agentcore invoke-agent-runtime --region "$REGION" \
    --agent-runtime-arn "$arn" \
    --runtime-session-id "$(session_id)" \
    --content-type application/json \
    --accept application/json \
    --cli-binary-format raw-in-base64-out \
    --payload "$payload" \
    "$out" >/dev/null || rc=$?

  if [[ "$rc" -ne 0 ]]; then
    echo "invoke failed (exit ${rc})" >&2
    rm -f "$out"
    return "$rc"
  fi

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
    # A task-shaped reply rather than a message-shaped one. Read every carrier
    # the spec allows: which one holds the answer depends on who built the
    # server, and reading only the obvious one is interop finding territory.
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

# Print a mesh reply the way the CLI used to: the consensus lines are the
# answer, the trailing envelope is what makes it checkable. Exit non-zero when
# no target reached consensus, so a totally failed run cannot exit 0 -- which is
# a defect this project has already shipped once.
#
# Takes a FILE, not a pipe. `python3 - <<'PY'` reads the *program* from stdin,
# so a piped reply is consumed by the interpreter and sys.stdin.read() returns
# "" -- which this reported as "no mesh_run envelope in the reply", i.e. as a
# fault in the master rather than in the reader. An error attributed to the
# wrong layer, in the tool built to stop exactly that.
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

# No `trap ... RETURN` here, deliberately. A RETURN trap in bash is **not**
# scoped to the function that sets it: it stays armed and fires on the next
# return of any function, where `$reply` is unset and `set -u` aborts the script.
# That killed `verify` after its first probe, mid-run, leaving the runtime wired
# to a single cloud -- a control harness that damages the thing it is measuring.
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

# Negative controls, run where the credentials actually are.
#
# AgentCore has no execution-time environment override -- unlike a Cloud Run
# job, where the same job spec could be run with one variable changed. So each
# probe here updates the runtime, waits for READY, invokes, and the caller
# restores the wiring at the end. Same image, same role, one variable changed;
# it is slower than the Cloud Run equivalent but it is the same control.
#
# Every probe isolates ONE leg with CURRENCY_MESH_CLOUDS. That is not tidiness:
# the mesh is a median and degrades on purpose, so a three-cloud run with one
# leg's credential removed still reaches quorum on the other two and exits 0.
# Read as a control, that exit code says "the denial was absorbed" while looking
# exactly like "there was no denial".
probe() {
  local label="$1" cloud="$2" expect="$3" override="${4:-}"
  local arn image account vars rc=0

  arn="$(runtime_arn)"; image="$(current_image)"; account="$(account_id)"
  # `if`, not `[[ ... ]] && echo`. A trailing && with a false test leaves the
  # whole group at status 1, and under `set -o pipefail` that fails the
  # pipeline, fails the assignment, and `set -e` then kills the run **with no
  # message at all** -- which it did, at the first positive probe, because a
  # positive probe is exactly the one with no override to append.
  vars="$(
    {
      peer_env
      echo "CURRENCY_MESH_CLOUDS=${cloud}"
      if [[ -n "$override" ]]; then echo "$override"; fi
    } | env_with_overrides
  )"

  echo
  echo "--- ${label}"
  update_runtime "$arn" "$image" "$account" --environment-variables "$vars"
  wait_ready || true
  run >/dev/null 2>&1 || rc=$?

  if [[ "$expect" == "deny" ]]; then
    [[ "$rc" -ne 0 ]] \
      && echo "    exit ${rc} -- denied, as required" \
      || echo "    exit 0 -- ANSWERED WITHOUT THE CREDENTIAL. This control failed:
    the leg's auth mode is a label, not a control."
  else
    [[ "$rc" -eq 0 ]] \
      && echo "    exit 0 -- answered, as required" \
      || echo "    exit ${rc} -- the POSITIVE control failed; the leg is broken
    independently of auth, and the denials below prove nothing until it is fixed."
  fi
}

verify() {
  echo "an authenticated leg is unproven without negative controls, and this"
  echo "mutates the live runtime once per probe. It ends by restoring the wiring."
  echo

  echo "1. unauthenticated, from here -- no SigV4 signature at all"
  local url; url="$(runtime_url)"
  echo "   invoke     -> $(curl -s -o /dev/null -w '%{http_code}' -m 30 -X POST "$url")   (expect 403)"
  echo "   agent card -> $(curl -s -o /dev/null -w '%{http_code}' -m 30 "${url}.well-known/agent-card.json")   (expect 403)"

  echo
  echo "2. positive controls -- each leg alone, credentials as deployed."
  echo "   These come first: a denial only means something once you know the"
  echo "   leg answers at all."
  probe "AWS leg, as deployed"   aws   allow
  probe "GCP leg, as deployed"   gcp   allow
  probe "Azure leg, as deployed" azure allow

  echo
  echo "3. negative controls -- each leg alone, credential removed."
  probe "AWS leg, auth mode forced to none"   aws   deny AWS_A2A_AUTH=none
  probe "GCP leg, auth mode forced to none"   gcp   deny GCP_A2A_AUTH=none
  probe "Azure leg, auth mode forced to none" azure deny AZURE_A2A_AUTH=none

  # Audience is caller-chosen, so this proves less than it looks like it does.
  # It still separates "the token was rejected" from "no token was sent".
  probe "GCP leg, right identity, wrong audience" gcp deny \
    GCP_A2A_AUDIENCE=https://not-this-service.example.com

  echo
  echo "restoring the wiring"
  wire >/dev/null
  wait_ready || true
  echo "done"
}

destroy() {
  local arn; arn="$(runtime_arn)" || arn=""
  if [[ "$arn" != "None" && -n "$arn" ]]; then
    aws bedrock-agentcore-control delete-agent-runtime --region "$REGION" \
      --agent-runtime-id "$(basename "$arn")" >/dev/null 2>&1 || true
  fi
  aws iam delete-role-policy --role-name "$ROLE_EXEC" --policy-name master-runtime 2>/dev/null || true
  aws iam delete-role --role-name "$ROLE_EXEC" 2>/dev/null || true
}

# The role ARN the other two clouds must trust. Printed rather than duplicated
# into their scripts, which read it from here.
role_arn() {
  aws iam get-role --role-name "$ROLE_EXEC" --query Role.Arn --output text
}

ensure_aws_credentials || exit 1

case "${1:-deploy}" in
  deploy) deploy ;;
  wire) wire ;;
  run) shift; run "${1:-EUR, JPY}" ;;
  matrix) matrix ;;
  verify) verify ;;
  url) runtime_url ;;
  env) peer_env ;;
  role-arn) role_arn ;;
  status) current_env ;;
  destroy) destroy ;;
  *) echo "usage: $0 {deploy|wire|run|matrix|verify|url|env|role-arn|status|destroy}" >&2; exit 2 ;;
esac
