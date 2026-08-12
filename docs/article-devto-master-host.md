---
title: "Hosting a Three-Cloud Agent Coordinator: Bedrock AgentCore vs Cloud Run"
published: false
description: The same three-cloud A2A mesh, run first from a Cloud Run coordinator and then from a Bedrock AgentCore one. What changes, what doesn't, and the platform gotchas that only show up once it's deployed.
tags: aws, googlecloud, ai, architecture
---

This is the same mesh I've been writing about for a while: three AI agents on
three clouds, each built with that vendor's own agent framework, each served
over [A2A v1.0](https://a2a-protocol.org) by that vendor's own stack.

- **Google** — an ADK agent on Cloud Run
- **AWS** — a Strands agent on Bedrock AgentCore Runtime
- **Azure** — an Agent Framework agent on Container Apps

Something has to call all three and reconcile the answers. That something is the
**coordinator**, and the question this post answers is where it should live.

I've now run it both ways — first from Cloud Run, then from Bedrock AgentCore
Runtime — with the three agents left exactly where they were. Same code, same
protocol, same credential seam. Only the host moved.

That turns out to be the useful experiment, because a coordinator's host decides
three things you'd probably rather decide on purpose: **what shape your
coordinator has to be, what it can prove about its own identity, and what it
pays before the first call.**

## The two topologies

```text
  Cloud Run coordinator            AgentCore coordinator
  (a run-to-completion job)        (a long-running server)
          |                                |
   +------+------+                  +------+------+
   |      |      |                  |      |      |
  GCP    AWS   Azure               GCP    AWS   Azure
  ADK  Strands  AF                 ADK  Strands  AF
       ^                                  ^
   in-cloud hop is GCP               in-cloud hop is AWS
```

Three agents, one coordinator, a **median** across the three answers so a single
divergent cloud can't move the result. The only difference between the two
diagrams is which box the coordinator sits in.

Here is the whole comparison up front:

| | Cloud Run coordinator | AgentCore coordinator |
|---|---|---|
| Shape | run-to-completion **job** | long-running **server** |
| Is the coordinator itself an agent? | no | **yes** |
| → GCP leg | metadata mint → ID token | `GetCallerIdentity` → Google STS → impersonate |
| → AWS leg | mint → `AssumeRoleWithWebIdentity` → SigV4 | SigV4 with the runtime's own role |
| → Azure leg | Entra Federated Identity Credential | **Entra client secret** |
| Legs with no stored secret | **3 of 3** | 2 of 3 |
| In-cloud hop | the GCP leg | the AWS leg |
| Fixed cost per run | ~1s (container start) | **~30ms** |
| Consensus latency, warm | 1953–2511ms | **549–623ms** |
| Negative controls | env override, non-destructive | mutates the live runtime |
| Interop matrix | 8/9 | 8/9 |

The rest of this post is those rows.

## Cloud Run runs jobs; AgentCore runs servers

This is the first fork and it's structural, not cosmetic.

A Cloud Run **job** runs to completion and exits. That's a natural fit for a
coordinator: fan out to three clouds, collect three answers, take the median,
print, exit. It's a script.

AgentCore Runtime hosts **servers**. There's no run-to-completion mode. So the
coordinator can't be a script — it has to sit there and answer something.

The way out is nicer than the constraint deserves: **make the coordinator an A2A
agent in its own right.** Same protocol in as out. It answers the same prompt
template its three peers answer and replies in the same wire format, on the same
contract as the AWS peer (port 9000, root path, ARM64, `GET /ping`).

Which means it's a drop-in participant. Any of the three client SDKs can drive
it, and its reply parses with the same parser the peers' replies use. Behind it
is three clouds and a median instead of a rate table, and nothing calling it has
to know that.

The reply carries a trailing envelope with the detail:

```json
{"mesh_run": {"clouds": ["gcp", "aws", "azure"], "auth_modes": {...}, "elapsed_ms": 617}}
```

Peers ignore it for free, because their parser only reads objects carrying a
`target_currency`. **A coordinator that speaks the same protocol it consumes can
be a peer in someone else's mesh.** That's a real architectural win, and I only
got it because AgentCore refused to run a job.

## The coordinator's host decides your auth story

This is the row that actually costs something, so it gets the most space.

Every callee in this mesh consumes external OIDC — AWS IAM OIDC providers, Entra
Federated Identity Credentials, AgentCore's `CUSTOM_JWT`. That part is symmetric.
The asymmetry is whether the **calling** runtime can mint an OIDC token.

**Cloud Run can.** It will mint a workload OIDC token for an arbitrary audience,
straight off the metadata server. So from Cloud Run, all three legs are keyless:

```bash
# from Cloud Run — audience is whatever the callee wants
curl -H "Metadata-Flavor: Google" \
  "http://metadata/computeMetadata/v1/instance/service-accounts/default/identity?audience=api://my-entra-app&format=full"
```

**AgentCore can't.** An AgentCore execution role is not an OIDC issuer. Outside
EKS/IRSA or Cognito, AWS will not mint a token for an arbitrary audience.

Follow that through leg by leg and you get one loss and one surprise.

### The loss: Azure

Entra's federated identity credential takes a **JWT assertion from an issuer it
can discover**, and nothing else. From Cloud Run there's a Google token to hand
it. From AgentCore there's nothing to federate with, so the leg carries an Entra
**client secret**.

It lives in AWS Secrets Manager and is read with the same role that signs the
other two legs, so it's never plaintext in the runtime's configuration. That
shrinks the blast radius. It doesn't remove the secret.

If "no long-lived credentials anywhere" is a requirement you've written down,
**that requirement is a constraint on your coordinator's host**, not on your
mesh. Worth knowing before you pick.

### The surprise: GCP stays keyless from AWS

This is the one I'd flag to anyone building the same shape, because it isn't
obvious and it saves the leg.

Google's Workload Identity Federation accepts an **AWS-shaped subject token**.
Not a JWT — a SigV4-signed `GetCallerIdentity` request, serialised and handed
over **unsent**, which Google replays against AWS STS to find out who signed it.

```
POST https://sts.googleapis.com/v1/token
  subject_token_type = urn:ietf:params:aws:token-type:aws4_request
  subject_token      = {"url":..., "method":"POST", "headers":[...]}
```

So AWS → GCP is keyless without a JWT existing anywhere in the exchange. It
sidesteps the exact problem that sinks the Azure leg.

**Don't generalise from it.** One provider accepts a replayable signed request;
the other demands an assertion from a discoverable issuer. They are not
comparable mechanisms, and the fact that one worked tells you nothing about the
other.

### Score

| Coordinator host | Keyless legs |
|---|---|
| Cloud Run | GCP ✓, AWS ✓, Azure ✓ — **3 of 3** |
| AgentCore | GCP ✓, AWS ✓, Azure ✗ — **2 of 3** |

## A server is faster than a job, mostly because it's already running

Warm consensus runs, three clouds, both hosts, all agreeing:

| Leg | From Cloud Run | From AgentCore |
|---|---|---|
| → GCP | 1138–1394ms | **485–494ms** |
| → AWS | 1028–1344ms | **274–332ms** |
| → Azure | 485–1532ms | 524–592ms |
| **Elapsed** | **1953–2511ms** | **549–623ms** |

Three things in that table are worth pulling out.

**The legs run concurrently, so elapsed tracks `max(legs)`, not `sum(legs)`.**
549ms elapsed against 1283ms of summed leg time. If you're sizing a timeout,
size it against your slowest cloud, not your total.

**The fixed cost is the hosting model.** A job pays container start on every
single run — about a second of it. A warm server pays **+25 to +35ms** over its
slowest leg. That gap is the coordinator's own overhead: agent-card fetches and
credential mints, which together cost tens of milliseconds. Everything else was
the box opening.

**The AWS leg got roughly 4× faster**, and that one's easy: it's an in-cloud hop
now, and there's no token exchange at all — just a signature over credentials the
runtime already holds.

The genuine surprise is the **GCP** leg. It got faster *despite* now paying two
network round trips before the call, where Cloud Run paid one metadata hop. More
work, less time. The token exchange was never what dominated that leg — the
distance was. If you're optimising a cross-cloud call, look at where the callee
is before you look at how you're authenticating to it.

Azure is the control, and it's the reason the other two rows mean anything: same
agent, same region, a different caller, no movement. Without a leg that doesn't
move, "everything got faster" is equally consistent with "I measured on a better
afternoon."

**Cold is a different regime and I won't average it in.** A cold Cloud Run agent
measured 15845ms against 485–494ms warm. An Azure leg elsewhere in this project
measured 23378ms cold against 441–570ms warm in the same session. Every number
above is warm.

## Whichever cloud hosts the coordinator, one leg stops being cross-cloud

Easy to miss, and it inflates your interoperability claim if you do.

The leg from the coordinator to its **own** cloud's agent is a hop inside one
vendor. Under the Cloud Run coordinator that was the GCP leg. Under AgentCore
it's the AWS leg. Two of three legs cross a vendor boundary either way — but
*which* two changed.

So the marker is read from the environment rather than hardcoded:

```bash
CURRENCY_COORDINATOR_CLOUD=aws
```

and the matrix stars that column and reports the two counts separately:

```text
client \ server  gcp               aws*              azure
a2a-sdk          ok 471ms          ok 258ms          ok 548ms
agent-framework  ok 415ms          ok 141ms          ok 512ms
google-adk       transport         ok 262ms          ok 581ms

8/9 attempted cells succeeded
  of which 5 crossed a cloud boundary and 3 did not
```

A marker you have to remember to edit is a marker that will be wrong exactly
once, at the worst possible time.

## The interop result doesn't care where the coordinator lives

Both topologies score **8/9**, and it's the same failing cell both times: **ADK's
own client cannot reach ADK's own server** when that server is hosted.

`to_a2a()` advertises the container's bind address in the agent card, and
`RemoteA2aAgent` believes it. Both halves pass Google's own tests, because
locally those two addresses are identical.

Getting the identical failure after moving the caller to an entirely different
cloud is useful: it confirms the defect is **in the card's advertised address,
not in who's dialling.** That's a fact about A2A implementations that survives
any hosting decision you make.

## Negative controls are easier on Cloud Run

If you care about *proving* your auth works rather than reporting that it does,
this row matters more than it looks.

A negative control here means: remove one leg's credential, confirm that leg is
denied. It has to isolate a single leg, because the mesh is a median and degrades
on purpose — a three-cloud run with one credential removed still reaches quorum
on the other two **and exits 0**. That reads as "no denial" and is nothing of the
kind.

| | Cloud Run | AgentCore |
|---|---|---|
| Mechanism | execution-time env override on the job spec | update runtime → wait READY → invoke → restore |
| Destructive? | no | **yes, while in flight** |
| Speed | seconds | minutes |
| Controls passing | 7 of 7 | 9 of 9 |

Cloud Run jobs take an environment override at execution time, so a control runs
the same job spec with one variable changed and touches nothing permanent.
AgentCore has no equivalent, so each probe **mutates the live runtime** and the
wiring gets restored afterwards.

One control also got weaker on AWS: `AWS_A2A_AUTH=none` on an in-cloud hop is
denied by AgentCore's own SigV4 requirement, not by a cross-cloud federation
refusing a caller. Same green tick, weaker statement.

## Gotchas

These are the ones that cost real time. None of them are discoverable locally.

**AgentCore hands your container only `AWS_REGION`.** No
`AWS_CONTAINER_CREDENTIALS_FULL_URI`, no `AWS_CONTAINER_CREDENTIALS_RELATIVE_URI`,
no keys. Credentials come from **IMDSv2** or they don't come at all. If your
resolver deliberately excludes IMDS — a defensible choice elsewhere, since a
silent IMDS fallback is how you pick up an instance profile nobody meant to grant
you — every leg fails identically before reaching any provider. Log the role name
IMDS serves, and "which identity did we sign with" becomes an observable instead
of an assumption.

**AgentCore sessions are sticky across a deploy.** A session binds to the
container it started in. Push a new image, keep a pinned session ID, and you are
talking to the *previous* build while the new one runs alongside it. Put the
runtime version in the session ID so a deploy rotates it. The failure mode
otherwise is testing the old build and believing it's the new one, which is worse
than a build that plainly doesn't work.

**Google matches `Authorization` case-sensitively; httpx lowercases header
names.** Build that subject-token header list by iterating
`httpx.Request.headers` and you get `authorization`, where Google looks up
`Authorization`. The error is:

```
invalid_grant: The given AWS request doesn't contain all the required headers.
```

Nothing is missing. A complete header list, reported as an incomplete one, naming
no header. Build the five headers by name.

**Google's pool sees the assumed-role ARN, not the role ARN you granted.**
`arn:aws:iam::123:role/foo` arrives as `arn:aws:sts::123:assumed-role/foo`.
Writing the granted ARN into the attribute condition is the commonest way this
fails, and it denies without naming the string mismatch.

**`x-goog-cloud-target-resource` has to be inside the signature**, not merely
present on the subject token. An unsigned one could be replayed at a different
pool, so Google refuses it — with a message that doesn't mention the header.

**The STS exchange yields an access token; Cloud Run wants an ID token.** Hence a
second hop through `generateIdToken`, and hence
`roles/iam.serviceAccountTokenCreator` on the impersonated service account.
Forget it and the 403 names the *service account*, which reads as a federation
failure when the federation already succeeded.

**Learn the error-code split on both sides.** Every provider here distinguishes
"I couldn't validate your token at all" from "I validated it and your conditions
didn't match," in different words:

| Provider | Setup is wrong | Conditions didn't match |
|---|---|---|
| AWS | `InvalidIdentityToken` | `AccessDenied` |
| Google | `invalid_grant` | `unauthorized_client` |

That split is the fastest debugging tool in the whole exercise. The Google
exchange is a single POST, so you can drive it from a laptop against the live
pool — with your own credentials the expected result is still a rejection, but by
the *attribute condition*. Watching the error move from `invalid_grant` to
`unauthorized_client` confirms a fix without a ten-minute image build.

Which is why the highest-value line of code in this project is: **log the raw
provider response at every auth boundary.** Not the status code — the body. An
earlier version of this work kept the status and threw the body away, and AWS had
been naming the missing IAM action in that body the whole time.

## Which one should you run?

| If you need | Pick |
|---|---|
| No stored secrets across heterogeneous callees | **Cloud Run** — it mints OIDC for any audience |
| Lowest latency to AWS-hosted agents | **AgentCore** — in-cloud hop, no exchange |
| Non-destructive negative controls | **Cloud Run** — execution-time env overrides |
| The coordinator to be callable as an agent | **AgentCore** — it hosts servers, so you build one |
| Lowest per-run fixed cost | **AgentCore** — a warm server skips container start |

The honest summary: **Cloud Run wins on identity, AgentCore wins on latency and
composability.** If your mesh spans vendors that each want their own token
format, the ability to mint OIDC for an arbitrary audience is worth more than the
milliseconds. If your callees are mostly on AWS, that calculus inverts fast.

## The part that didn't move

The stack underneath is unchanged. Three agents, three vendors' frameworks,
three serving stacks, one median. What moved was one container, from one cloud to
another.

The reason that was a one-week change rather than a rewrite is a single function:

```python
credentials_for(peer, endpoint) -> httpx.Auth | None
```

Every leg asks that one question, and the answer is attached to the **client**
rather than to the request — so the agent-card fetch gets authenticated too, not
just the invoke. Moving the coordinator from Cloud Run to AgentCore replaced all
three implementations behind it and **touched no caller.** The CLI, the mesh, the
matrix and the coordinator itself all still ask the same question and get an
`httpx.Auth` back.

Three clouds, two coordinator hosts, one seam. That's the part worth stealing.

---

Everything is on GitHub:
[**xbill9/multicloud-adk-a2a-currency-aws**](https://github.com/xbill9/multicloud-adk-a2a-currency-aws).
The mesh runs on a laptop in about a minute — three agents on three ports, the
full 3×3 matrix, and a demo where one cloud goes offline and then one cloud
*lies*, so you can watch the median hold and name the outlier.

The predecessor series — six directed edges between AgentCore, Foundry and ADK —
is at [xbill9/cross-cloud-a2a-rollup](https://github.com/xbill9/cross-cloud-a2a-rollup).

If you know whether **AgentCore Runtime can mint a workload OIDC token for an
arbitrary audience**, I'd like to hear it. That one capability is the whole
difference between the two rows in the keyless table.
