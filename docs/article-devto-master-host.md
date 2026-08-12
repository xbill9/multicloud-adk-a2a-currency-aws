---
title: "Hosting a Three-Cloud Agent Coordinator: Bedrock AgentCore vs Cloud Run"
published: false
description: The same three-cloud A2A mesh, run first from a Cloud Run coordinator and then from a Bedrock AgentCore one. What changed, what didn't, and the platform details that only showed up after deploying.
tags: aws, googlecloud, ai, architecture
---

This is the same mesh I've been writing about for a while: three AI agents on
three clouds, each built with that vendor's own agent framework, each served
over [A2A v1.0](https://a2a-protocol.org) by that vendor's own stack.

- **Google** — an ADK agent on Cloud Run
- **AWS** — a Strands agent on Bedrock AgentCore Runtime
- **Azure** — an Agent Framework agent on Container Apps

Something has to call all three and reconcile the answers. That something is the
**coordinator**, and this post is about where it lives.

I've run it both ways now — first on Cloud Run, then on Bedrock AgentCore
Runtime — with the three agents left where they were. Same code, same protocol,
same credential seam. Only the coordinator's host changed.

| | Cloud Run coordinator | AgentCore coordinator |
|---|---|---|
| Deployed as | a Cloud Run job | an AgentCore server |
| → GCP leg | metadata mint → ID token | `GetCallerIdentity` → Google STS → impersonate |
| → AWS leg | mint → `AssumeRoleWithWebIdentity` → SigV4 | SigV4 with the runtime's own role |
| → Azure leg | Entra Federated Identity Credential | **Entra client secret** |
| Legs with no stored secret | 3 of 3 | 2 of 3 |
| In-cloud hop | the GCP leg | the AWS leg |
| Fixed cost per run | ~1s | ~30ms |
| Consensus latency, warm | 1953–2511ms | 549–623ms |
| Interop matrix | 8/9 | 8/9 |

## Authentication is where the hosts actually differ

Every callee in this mesh consumes external OIDC — AWS IAM OIDC providers, Entra
Federated Identity Credentials, AgentCore's `CUSTOM_JWT`. That part is
symmetric. The asymmetry is whether the *calling* runtime can mint an OIDC
token.

Cloud Run can. It mints a workload OIDC token for an arbitrary audience off the
metadata server:

```bash
curl -H "Metadata-Flavor: Google" \
  "http://metadata/computeMetadata/v1/instance/service-accounts/default/identity?audience=api://my-entra-app&format=full"
```

AgentCore can't. An execution role is not an OIDC issuer, and outside EKS/IRSA
or Cognito, AWS won't mint a token for an arbitrary audience.

That plays out differently on each leg.

### Azure loses its federation

Entra's federated identity credential takes a JWT assertion from an issuer it
can discover, and nothing else. From Cloud Run there was a Google token to hand
it. From AgentCore there's nothing to federate with, so the leg carries an Entra
**client secret**.

It sits in AWS Secrets Manager and is read with the same role that signs the
other two legs, so it's never plaintext in the runtime's configuration. That
shrinks the blast radius; it doesn't remove the secret.

"No long-lived credentials anywhere" turned out to be a property of Cloud Run's
token minting rather than of the mesh. Nothing about the mesh changed and the
claim still went from three legs to two.

### GCP stays keyless by an unexpected route

Google's Workload Identity Federation accepts an AWS-shaped subject token. Not a
JWT — a SigV4-signed `GetCallerIdentity` request, serialised and handed over
*unsent*, which Google replays against AWS STS to see who signed it.

```
POST https://sts.googleapis.com/v1/token
  subject_token_type = urn:ietf:params:aws:token-type:aws4_request
  subject_token      = {"url":..., "method":"POST", "headers":[...]}
```

So AWS → GCP stays keyless with no JWT anywhere in the exchange, which sidesteps
the problem that sank the Azure leg.

It's specific to these two providers. Google accepts a replayable signed
request; Entra wants an assertion from a discoverable issuer. The GCP leg
working says nothing about whether the Azure one could.

## Latency

Warm consensus runs, three clouds, both hosts, all agreeing:

| Leg | From Cloud Run | From AgentCore |
|---|---|---|
| → GCP | 1138–1394ms | 485–494ms |
| → AWS | 1028–1344ms | 274–332ms |
| → Azure | 485–1532ms | 524–592ms |
| **Elapsed** | **1953–2511ms** | **549–623ms** |

The legs run concurrently, so elapsed tracks `max(legs)` rather than
`sum(legs)` — 549ms elapsed against 1283ms of summed leg time.

Above `max(legs)`, the Cloud Run job paid about a second, because a job starts a
container per execution. The AgentCore server pays +25 to +35ms, being already
up. That +30ms is the coordinator's own work — agent-card fetches and credential
mints come to tens of milliseconds, not hundreds.

The per-leg rows are measured from inside the coordinator, so they exclude
start-up either way:

- **AWS, 274–332ms against 1028–1344ms.** In-cloud now, and no token exchange at
  all — just a signature over credentials the runtime already holds.
- **GCP, 485–494ms against 1138–1394ms.** Faster, which surprised me, because
  this version pays two network round trips before the call where the old one
  paid a single metadata hop. The exchange wasn't what dominated that leg; the
  distance was.
- **Azure, 524–592ms against 485–1532ms.** Unchanged within noise — same agent,
  same region, different caller.

Azure holding still is the only reason I trust the other two rows. Without a leg
that didn't move, "everything got faster" would be equally consistent with a
quieter afternoon on the network.

Cold is a separate regime and I haven't averaged it in. A cold Cloud Run agent
measured 15845ms against 485–494ms warm, and an Azure leg elsewhere in this
project measured 23378ms cold against 441–570ms warm in the same session. Every
number above is warm.

## One leg is never cross-cloud

The leg from the coordinator to its own cloud's agent is a hop inside a single
vendor. Under the Cloud Run coordinator that was the GCP leg; under AgentCore
it's the AWS leg. Two of three cross a vendor boundary either way, but which two
changed — so the marker comes from the environment rather than being hardcoded:

```bash
CURRENCY_COORDINATOR_CLOUD=aws
```

The matrix stars that column and reports the counts separately:

```text
client \ server  gcp               aws*              azure
a2a-sdk          ok 471ms          ok 258ms          ok 548ms
agent-framework  ok 415ms          ok 141ms          ok 512ms
google-adk       transport         ok 262ms          ok 581ms

8/9 attempted cells succeeded
  of which 5 crossed a cloud boundary and 3 did not
```

The cross-cloud count went from 6+2 to 5+3, which looks like a regression and
isn't: the failing cell used to sit inside the starred column and now sits
outside it, so one more of the eight passing cells is in-cloud.

## The interop result didn't move

Both topologies score 8/9, and it's the same failing cell: ADK's own client
can't reach ADK's own server when that server is hosted. `to_a2a()` advertises
the container's bind address in the agent card and `RemoteA2aAgent` believes it.
Both halves pass Google's own tests, because locally those two addresses are
identical.

Getting the identical failure after moving the caller to another cloud was
useful — it puts the defect in the card's advertised address rather than in
anything about the caller.

## The coordinator is now an agent itself

AgentCore hosts servers and has no run-to-completion mode, so the coordinator
became an A2A agent in its own right — same protocol in as out.

It answers the same prompt template its three peers answer and replies in the
same wire format, on the same contract as the AWS peer (port 9000, root path,
ARM64, `GET /ping`). Any of the three client SDKs can drive it, and its reply
parses with the same parser the peers' replies use. Behind it is three clouds
and a median; nothing calling it needs to know that.

The reply carries a trailing envelope with the detail:

```json
{"mesh_run": {"clouds": ["gcp", "aws", "azure"], "auth_modes": {...}, "elapsed_ms": 617}}
```

Peers ignore it, because their parser only reads objects carrying a
`target_currency`. The upshot is that the coordinator can sit inside someone
else's mesh as an ordinary peer.

## Negative controls

A negative control here means removing one leg's credential and confirming that
leg is denied. It has to isolate a single leg: the mesh is a median and degrades
by design, so a three-cloud run with one credential removed still reaches quorum
on the other two and exits 0.

| | Cloud Run | AgentCore |
|---|---|---|
| Mechanism | execution-time env override | update runtime → wait READY → invoke → restore |
| Destructive | no | yes, while in flight |
| Speed | seconds | minutes |
| Controls passing | 7 of 7 | 9 of 9 |

A Cloud Run job takes an environment override at execution time, so a control
runs the same spec with one variable changed and leaves nothing behind.
AgentCore has no equivalent, so each probe updates the live runtime and restores
the wiring afterwards.

One control also proves less than it used to. `AWS_A2A_AUTH=none` on an in-cloud
hop is denied by AgentCore's own SigV4 requirement, not by a cross-cloud
federation refusing a caller.

## What deploying turned up

None of these were visible locally against a green 127-test suite.

**AgentCore hands the container only `AWS_REGION`.** No
`AWS_CONTAINER_CREDENTIALS_FULL_URI`, no
`AWS_CONTAINER_CREDENTIALS_RELATIVE_URI`, no keys — credentials come from IMDSv2
or not at all. My resolver had deliberately excluded IMDS, on the reasoning that
a silent fallback is how you end up signing with an instance profile nobody
meant to grant you. On this runtime there's no other source, so all three legs
failed identically before reaching any provider. IMDS is now the last resort,
and the resolver logs which role it got, which covers the original worry better
than excluding it did.

**AgentCore sessions are sticky across a deploy.** A session binds to the
container it started in. I pushed a new image, kept a pinned session ID, and
spent a while looking at an error string I'd already deleted from the source
while a different log stream showed the new code starting cleanly. The session
ID now carries the runtime version so a deploy rotates it.

**Google matches `Authorization` case-sensitively, and httpx lowercases header
names.** Building the subject-token header list by iterating
`httpx.Request.headers` yields `authorization`, where Google looks up
`Authorization`. The rejection is:

```
invalid_grant: The given AWS request doesn't contain all the required headers.
```

Nothing was missing. Building the five headers by name fixed it, and also
dropped a `content-length: 0` that httpx had been contributing to a signature
that never covered it.

**Google's pool sees the assumed-role ARN, not the role ARN.**
`arn:aws:iam::123:role/foo` arrives as `arn:aws:sts::123:assumed-role/foo`, and
putting the granted ARN in the attribute condition denies without naming the
mismatch.

**`x-goog-cloud-target-resource` has to be inside the signature**, not just
present on the subject token — an unsigned one could be replayed at another
pool. The refusal doesn't mention the header.

**The STS exchange returns an access token and Cloud Run wants an ID token**,
hence a second hop through `generateIdToken` and
`roles/iam.serviceAccountTokenCreator` on the impersonated service account.
Without it the 403 names the service account, which reads like a federation
failure when the federation already worked.

### The error codes are the fast path

Both providers distinguish "couldn't validate your token at all" from
"validated it, and your conditions didn't match":

| Provider | Setup wrong | Conditions didn't match |
|---|---|---|
| AWS | `InvalidIdentityToken` | `AccessDenied` |
| Google | `invalid_grant` | `unauthorized_client` |

That split is what made the header bug quick to fix. The Google exchange is a
single POST, so I could drive it from a laptop against the live pool — with my
own credentials the expected result is still a rejection, but by the attribute
condition rather than the token's shape. Watching the error move from
`invalid_grant` to `unauthorized_client` confirmed the fix without a ten-minute
image build.

Both codes are only readable because the auth layer logs the raw provider
response body rather than just the status. An earlier version of this work kept
the status and discarded the body, and AWS had been naming the missing IAM
action in that body the whole time.

## Pros and cons, plainly

### Cloud Run as the coordinator

**What's good**

- **It can get a token for anything.** Cloud Run mints an OIDC token for
  whatever audience you name. That's what let all three legs run with nothing
  stored. If your mesh authenticates to several providers that each want their
  own token format, this is the most valuable item on either list.
- **Proving your auth works is cheap and safe.** Override one environment
  variable at execution time, watch that leg get denied, and nothing is left
  changed. Seconds per probe.
- **You choose the shape.** A service is a warm, always-on server; a job starts,
  runs and exits. AgentCore only does the first.

**What's not**

- **Every call to an AWS agent is long distance.** The AWS leg measured
  1028–1344ms from Cloud Run against 274–332ms from AgentCore.
- **A job pays container start on every run** — about a second here. A service
  with a warm instance avoids that, at the cost of the easy negative controls
  above.
- **Your GCP numbers stop counting as interop.** The coordinator and the GCP
  agent share a cloud, so that leg is an in-cloud hop.

### AgentCore as the coordinator

**What's good**

- **The AWS leg gets very cheap.** Same cloud, same account, so it's SigV4 with
  the role the runtime already holds — no token exchange at all. 274–332ms.
- **It's already running.** About 30ms of fixed cost per run instead of a
  second.
- **You still reach GCP with no secret**, because Google accepts an AWS-signed
  request as proof of who you are.
- **The coordinator ends up addressable**, since you build it as a real agent
  and other things can then call it.

**What's not**

- **It can't get a token for Azure, and you can't fix that from here.** No OIDC
  issuer means no Entra federation, so the leg carries a client secret. This is
  the biggest single drawback on either side.
- **Proving your auth works is slow and destructive.** Every negative control
  updates the live runtime, waits for it to come back, runs, and puts it back.
  Minutes rather than seconds, and the runtime is misconfigured while it happens.
- **Sessions stick to old builds.** Deploy a new image with a pinned session ID
  and you'll keep talking to the previous container.
- **Credentials come from IMDS or nowhere.** The container is handed
  `AWS_REGION` and nothing else.
- **No batch mode.** Servers only.

### Picking one

- **Most of your agents on AWS?** AgentCore. The latency win is real and the
  stored secret is confined to one leg.
- **Agents spread across vendors, especially Azure?** Cloud Run. Minting a token
  for any audience is worth more than the milliseconds.
- **Need to re-prove your auth regularly?** Cloud Run, by a wide margin.
- **Want the coordinator callable by other agents?** Either.

## What didn't change

Three agents, three vendors' frameworks, three serving stacks, one median. The
coordinator moved; none of that did.

The reason it was a week's work rather than a rewrite is one function:

```python
credentials_for(peer, endpoint) -> httpx.Auth | None
```

Every leg asks that, and the answer attaches to the client rather than the
request, so the agent-card fetch is authenticated along with the invoke. Moving
the coordinator replaced all three implementations behind it and touched no
caller — the CLI, the mesh, the matrix and the coordinator itself still ask the
same question and get an `httpx.Auth` back.

---

Everything is on GitHub:
[**xbill9/multicloud-adk-a2a-currency-aws**](https://github.com/xbill9/multicloud-adk-a2a-currency-aws).
The mesh runs on a laptop in about a minute — three agents on three ports, the
full 3×3 matrix, and a demo where one cloud goes offline and then one cloud
*lies*, so you can watch the median hold and name the outlier.

The predecessor series — six directed edges between AgentCore, Foundry and ADK —
is at [xbill9/cross-cloud-a2a-rollup](https://github.com/xbill9/cross-cloud-a2a-rollup).

And if anyone knows whether AgentCore Runtime can mint a workload OIDC token for
an arbitrary audience, I'd like to hear it — that one capability is the whole
difference between two keyless legs and three.
