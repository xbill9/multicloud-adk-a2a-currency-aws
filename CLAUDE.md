# Working notes for this repo

## Ground rule: deploy, then document

Do not write article or results content for a path that has not been deployed
and exercised end to end. Code-complete plus a green local suite is not a
result. This project's own history is the argument: in the six-edge predecessor
series, the last build was code-complete with a passing suite for a day, and
deploying it surfaced six defects — five of which no local test could have
caught.

All three clouds are now deployed and exercised together (2026-08-07). The rule
did not stop applying when that became true — it changed shape. What the README
must stay honest about now is the *gap between deployed and measured*: which
legs have negative controls behind them, which numbers are single cold runs,
and which claims still rest on a hermetic test rather than a provider response.

There is a second lesson from this project's own history, and it cost more than
any defect: **work that is deployed but not committed does not exist.** The AWS
and Azure legs were built, deployed and run on 2026-08-02, and none of it was
in git — it was recovered a week later out of a Cloud Build source tarball, and
the tests written that day were gone for good, because `.gcloudignore` excludes
`tests/` and `docs/`. Deploy, then document, then *commit*.

The repo has been in git since 2026-08-12, pushed public to
[`xbill9/multicloud-adk-a2a-currency-aws`](https://github.com/xbill9/multicloud-adk-a2a-currency-aws)
as a single initial commit. The rule stands; the recovery story is now history
rather than the current state, and there is a remote to push to.

## Ground rule: no virtualenvs, latest everything

**Never create or use a virtualenv.** No `uv venv`, no `python -m venv`, no
`.venv`. Install to the system interpreter with `uv pip install --system` and
run with `python3 -m pytest`. If a doc anywhere still tells you to make one,
that doc is wrong and should be fixed rather than followed.

**Use the latest version of every package, runtime, and compiler.** Latest
Python, latest base images, latest SDKs. Do not pin defensively, do not carry a
pin forward because it was once true, and do not install an old version because
a document mentions one.

**When the latest stack breaks something, fix what broke.** Do not downgrade.
Pinning back is not a fix, it is a deferral — the break is still there, it now
fires at a worse moment, and by then the cause is buried under however many
releases were skipped. Chasing old packages is how a project accumulates
problems it cannot date.

The one legitimate reason to pin is a **measured** failure you cannot fix from
here, and such a pin owes two things: a comment saying which failure, and a
re-test against latest whenever the area is touched. A pin nobody re-tests is
indistinguishable from rot.

This repo had two such pins and both are now gone. `google-adk==2.5.0` and
`a2a-sdk==1.1.2` existed because `google-adk` 2.4.0 imports an `a2a-sdk` 1.x
removal (finding 4 in `docs/INTEROP.md`). Retested 2026-08-02: `to_a2a` imports
and serves on `google-adk` 2.6.1, the suite passes, and the pins are removed
from `pyproject.toml`, both Dockerfiles and the README.

Two lessons from that, worth applying to the next pin:

- **`a2a-sdk==1.1.2` was never load-bearing** — 1.1.2 is just the latest
  release, so the pin was redundant and had been repeated across four files as
  though it were a finding.
- **"latest of each is not a safe assumption" was true on a date and then read
  as a law.** That is exactly how a pin outlives its defect. Write the *measured
  failure* into the comment, never the general warning.

## Running the suite locally

`python3 -m pytest tests/ -q`. Hermetic by default: the eleven
`tests/test_live_mesh.py` cells skip unless the local mesh is up.

**The skip guard is a port check on :10001–10003, not an identity check**, and
a sibling project serves the same three module paths (`agents.gcp.server` and
friends) on the same three ports. If `~/multicloud-a2a-subagent`'s mesh is
running, `./infra/run_mesh.sh status` reports `up`, the live cells execute
against *its* agents, and they fail rather than skip. Measured 2026-08-12:
`11 failed, 127 passed`, the responses carrying an `a2a-research` marker.
Before believing a live-mesh failure belongs to this repo, check whose it is:
`ss -ltnp | grep 1000` then `readlink /proc/<pid>/cwd`.

## Cross-cloud auth: the plan

Every callee here consumes external OIDC — AWS IAM OIDC providers, Entra
Federated Identity Credentials, AgentCore `CUSTOM_JWT`. The asymmetry is
whether the *calling* runtime can mint an OIDC token.

**The master runs on Bedrock AgentCore Runtime** (`coordinator/master.py`,
`infra/deploy_master_aws.sh`), moved there from Cloud Run on 2026-08-12. It is
an A2A agent like its three peers rather than a job, because AgentCore hosts
servers.

| Leg | Mechanism | Keyless | Status (2026-08-12) |
|---|---|---|---|
| AWS → GCP | signed `GetCallerIdentity` → Google STS → impersonate | yes | deployed, 485–494ms warm |
| AWS → AWS | SigV4 with the runtime's own role | yes | deployed, 274–332ms; **in-cloud hop** |
| AWS → Azure | Entra client secret from Secrets Manager | **no** | deployed, 524–592ms warm |

Three facts about the platform, all measured by deploying and none discoverable
locally. **AgentCore Runtime gives its container only `AWS_REGION`** — no
container-credential endpoint, no keys — so credentials come from IMDSv2, and
the role it serves is logged by name. **AgentCore sessions are sticky across a
deploy**, so a pinned session ID keeps reaching the microVM it started on: the
session ID must carry the runtime version or you test the previous build.
**Google matches `Authorization` case-sensitively** while httpx lowercases
header names, so a complete subject-token header list is refused as
`invalid_grant: doesn't contain all the required headers`.

**"No long-lived secrets anywhere in the mesh" is no longer achievable, and
that is the finding rather than a regression.** It was true under the Cloud Run
master — measured, 2026-08-07 — because Cloud Run mints workload OIDC for an
arbitrary audience. The AWS-rooted mesh was measured on 2026-08-12: `3/3 clouds,
agreed`, matrix 8/9, nine controls for nine, and **consensus at
`max(legs) + ~30ms`** — which corrects the old `+~1s`, almost all of which was
the Cloud Run job's container start rather than the card fetches and mints it
had been attributed to. An AgentCore execution role is not an OIDC issuer, Entra's
FIC takes nothing but a JWT assertion, and outside EKS/IRSA or Cognito AWS will
not mint one. So the property belonged to *the host*, not to the mesh, which is
exactly what moving one variable was meant to establish.

AWS → GCP survives keyless only because Google accepts an AWS-shaped subject
token — a signed-but-unsent request it replays itself — so that leg never needs
a JWT at all. Do not generalise from it to Azure; the mechanisms are not
comparable.

Anything claiming a secretless mesh must now say **two of three legs**, and
must not describe `entra-client-secret` as though it were the `entra-fic` it
replaced. `MeshRun.auth_modes` reports the mode per leg for this reason.

### Constraints that cost real time before

The first four applied to the Cloud-Run-rooted legs, which no longer exist
here. They are kept because they are about the *providers*, not about this
repo's topology, and the next project to point a Google identity at AWS or
Entra will meet all four again.

- **Audience alone is not authorization.** Audience is caller-chosen, so an
  audience-only condition proves only that *some* identity in that IdP minted
  the token. Always also pin the subject, using the immutable numeric ID rather
  than an email, which can be freed and re-bound. The AWS-rooted equivalent is
  the pool's attribute condition: pin `attribute.aws_role`, because a provider
  scoped only to an account ID trusts every identity in it.
- **AWS federates with `accounts.google.com` natively.** Creating an explicit
  IAM OIDC provider for it *breaks* federation (`InvalidIdentityToken`). For
  Entra you must create one. Opposite rules, same-looking task.
- **The IAM condition keys do not mean what they are named.**
  `accounts.google.com:oaud` is the token's `aud`; `accounts.google.com:aud` is
  the token's `azp` (a number). Putting an audience string in `:aud` can never
  match.
- **`format=full`** on the GCP metadata mint, or Google trims the token and
  omits the `email` claim.
- **Foundry's incoming A2A accepts Entra and only Entra** — no custom issuer.
- **Diagnostic, AWS side:** `InvalidIdentityToken` means the token could not be
  validated at all; `AccessDenied` means your trust conditions did not match.
  That distinction separates a provider-setup bug from a condition bug.

Now current, for the AWS-rooted legs:

- **Google's pool sees the *assumed-role* ARN**, not the role ARN you granted:
  `arn:aws:iam::123:role/foo` arrives as `arn:aws:sts::123:assumed-role/foo`.
  Writing the granted ARN into the attribute condition is the commonest way this
  fails, and it denies with `permission_denied` rather than anything naming the
  string mismatch.
- **`x-goog-cloud-target-resource` must be inside the signature**, not merely
  present on the subject token. An unsigned one could be replayed at a different
  pool, so Google refuses it — with a message that does not mention the header.
- **The STS exchange yields an access token, and Cloud Run wants an ID token.**
  Hence the second hop, `generateIdToken`, and hence
  `roles/iam.serviceAccountTokenCreator` on the impersonated SA. Forget it and
  the 403 names the *service account*, reading as a federation failure when the
  federation already succeeded.
- **Diagnostic, Google side:** `invalid_grant` means the subject token was
  rejected outright (provider setup); `invalid_request` means it was read and
  the request was malformed; `permission_denied` means the caller was
  identified and the attribute condition rejected it. Same three-way split as
  the AWS side, different words.

### Where auth belongs in this codebase

Done, and it has now survived a change of host, which is the test that matters:
`credentials_for(peer, endpoint) -> httpx.Auth | None` in `coordinator/auth.py`,
re-exported from `coordinator/participants.py`. Moving the master from Cloud Run
to AgentCore replaced all three implementations and touched no caller — the
CLI, the mesh, the matrix and the master all still ask the same question.

The split is worth keeping: `auth.py` holds what every leg shares (the SigV4
signer, the expiry caches, the provider logging, the registry),
`aws_origin.py` holds the three implementations. Keep the registry in `auth.py`
whatever happens next, so one function still answers "how does this leg
authenticate" regardless of which cloud the master is in.

**Log the raw provider response at every auth boundary.** This is worth more
than the federation work itself. In the predecessor series nothing cost more
time than unreadable auth errors: an adapter that reported only an HTTP status
and discarded the STS body, and an error string that travelled back as a tool
result and got paraphrased by the model into "an issue with the web identity
token." Raise *and* log; the raised message is not an observable.

## Topology

The N-way median consensus in `coordinator/consensus.py` is the right design —
keep it. A primary/verifier pair reintroduces a privileged source whose failure
is unrecoverable; the median means one divergent cloud cannot move the answer.

Whichever cloud the master runs in, the leg to that cloud's agent is an
**in-cloud hop, not cross-cloud**. Label it in the matrix rather than letting it
pad the interop claim. It was the `gcp` column under the Cloud Run master and is
the `aws` column under the AgentCore one, which is why
`CURRENCY_COORDINATOR_CLOUD` is read from the environment and never hardcoded —
a marker that has to be edited when the host moves is a marker that will be
wrong the once it matters.

## Evidence from the predecessor series

Six directed edges between Bedrock AgentCore, Microsoft Foundry, and Google ADK,
all deployed and measured as of 2026-07-31. Summary report:
[xbill9/cross-cloud-a2a-rollup](https://github.com/xbill9/cross-cloud-a2a-rollup).

Relevant measured results:

- The A2A leg tracks the *remote's model and runtime*, not the protocol or the
  distance: 1.7–2.1 s to a Cloud Run container, 18.8–25.1 s to either hosted
  agent runtime. Expect the same shape here once agents are hosted — the
  current single-digit-millisecond matrix numbers are local, direct-brain.
- Verified/consensus latency ≈ max(legs), not the sum, when calls are issued
  concurrently. Measured here 2026-08-07 and the ≈ needs a term: elapsed is
  `max(legs) + ~1s` of coordinator fixed cost (container start, card fetches,
  credential mints), which no per-leg figure includes. Not the sum — but
  quoting max(legs) alone was wrong by 85% on the fastest of three warm runs.
- ~~Open question nobody has answered: on ADK → AgentCore, scoping
  `bedrock-agentcore:InvokeAgentRuntime` to `runtime/<id>` and `runtime/<id>/*`
  was denied 403 on the agent-card fetch; only `Resource: "*"` worked.~~
  **Answered 2026-08-07, and the question was aimed at the wrong field.** The
  card fetch is a separate *action*, `bedrock-agentcore:GetAgentCard`, which no
  policy had granted at any scope; widening `Resource` worked by accident. Both
  narrow resources are sufficient and this repo ships them. Treat the
  predecessor's "only `Resource: '*'` works" as retired, not repeated. The grant
  moved with the master — it sits on the master's execution role now
  (`deploy_master_aws.sh`), not on a federated role.
