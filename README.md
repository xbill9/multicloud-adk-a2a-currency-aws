# Three-cloud A2A currency mesh

A proof of concept that links **native agents from Google Cloud, AWS, and
Azure** over A2A v1.0 — each built with its own vendor's agent framework, each
serving A2A through its own vendor's stack — and makes them answer one
question together.

It exists to answer a question the two-cloud version could not: **does A2A
actually interoperate between vendors, or does every pair need a workaround?**

Short answer: locally, all nine client/server pairs work — two only after
working around defects that neither vendor's own tests would catch. Deployed on
all three vendors' hosting, it is **8/9**: against the hosted GCP agent, **ADK's
own client cannot reach ADK's own server**, because `to_a2a()` advertises the
container's bind address and `RemoteA2aAgent` believes it. Both halves pass
Google's tests, because locally those two addresses are the same.

**The master has moved from Cloud Run to Bedrock AgentCore Runtime** (deployed
2026-08-12, `3/3 clouds, agreed`, hosted matrix 8/9). Numbers dated before that
belong to the Cloud-Run-rooted mesh and are labelled where they appear. See
[step 5 of the deployment
plan](docs/DEPLOYMENT_PLAN.md#5-move-the-master-to-bedrock--deployed-2026-08-12)
for what changed, what it cost, and the four defects deploying it found. The
short version is two things:

- **Two of the three legs are keyless; the third is not.** The AWS→Azure leg now
  carries an Entra client secret, because Entra's federated credential wants a
  JWT assertion and an AgentCore execution role cannot mint one. It is held in
  Secrets Manager and read with the master's own role, which shrinks the blast
  radius but does not restore the claim. Under the Cloud Run master all three
  were keyless, and that was a property of *the host*, not of the mesh.
- **The in-cloud hop moved.** It used to be the GCP leg — Cloud Run reaching
  Cloud Run. It is now the AWS leg, AgentCore reaching AgentCore. Two of the
  three legs cross a vendor boundary either way, and the matrix marks the one
  that does not rather than letting it pad the score.

Each leg has a negative control behind it rather than a self-reported mode.

**Prior art.** A search on 2026-08-09 did not turn up cross-vendor A2A interop
testing. The closest work is adjacent: the official
[`a2a-tck`](https://github.com/a2aproject/a2a-tck) validates a single
implementation against the *spec*, locally;
[`a2a-inspector`](https://github.com/a2aproject/a2a-inspector) validates one
agent; and [A2A issue #1755](https://github.com/a2aproject/A2A/issues/1755)
measured 50 agents advertising A2A, of which 100% advertised and 0% answered a
correct `tasks/send` — reachability at breadth, not interop at depth. The A2A
roadmap lists expanded testing as planned. The nearest actual prior art is this
author's own [six-edge predecessor
series](https://github.com/xbill9/cross-cloud-a2a-rollup), which covered
protocol versions, dependency extras, identity federation, header forwarding
and URL shapes across three clouds pairwise. This is a basic search, not a
systematic review, so read it as *no cross-vendor interop matrix was found*
rather than none exists.

See [`docs/INTEROP.md`](docs/INTEROP.md) and
[`docs/DEPLOYMENT_PLAN.md`](docs/DEPLOYMENT_PLAN.md).
[`docs/FOLLOWUP.md`](docs/FOLLOWUP.md) is the critical read: what this project
demonstrates versus what it says it demonstrates, and the work that would close
the gap. None of it is done.

## The demo

One command, four acts — the last two are the point:

```console
$ ./infra/demo.sh

1. Three clouds, one question       three vendors' frameworks, one answer
2. The interop matrix               3 client SDKs x 3 hosted agents
3. A cloud goes offline             the median degrades instead of failing
4. A cloud lies                     the median holds; the outlier is named
```

Any demo can show three green ticks. The claim this project makes is about
what happens when a participant is *wrong*, which is why acts 3 and 4 exist:

```console
100 USD = 92 EUR @ 0.92 [3/3 clouds, DISAGREED]
    gcp                  92 (229ms)
    aws               124.2 (25ms)
    azure                92 (24ms)
  warning: 1 of 3 clouds disagree by more than 0.50% (aws)
```

## The matrix

```console
$ ./infra/run_mesh.sh start
$ python3 -m matrix.runner

A2A interop matrix  (100 USD -> EUR, GBP, brain=direct)

client \ server  gcp               aws               azure
-----------------------------------------------------------------------
a2a-sdk          ok 163ms          ok 9ms            ok 9ms
agent-framework  ok 135ms          ok 7ms            ok 8ms
google-adk       ok 922ms          ok 8ms            ok 8ms

9/9 attempted cells succeeded
```

Latencies are loopback, direct-brain, single runs on one machine — they order
the stacks and nothing more. An earlier revision of this table recorded
69/31/864ms for the `gcp` column on different hardware and an older ADK.

When `CURRENCY_COORDINATOR_CLOUD` is set, the same table marks the column that
never left that cloud and reports the interop score separately from the raw
one:

```console
$ CURRENCY_COORDINATOR_CLOUD=gcp python3 -m matrix.runner --client a2a-sdk

client \ server  gcp*              aws               azure
----------------------------------------------------------------------
a2a-sdk         ok 153ms          ok 7ms            ok 8ms

3/3 attempted cells succeeded
  of which 2 crossed a cloud boundary and 1 did not
* in-cloud hop: gcp shares the coordinator's cloud, so these cells do not
  support the interop claim
```

`deploy_master_aws.sh` sets that variable to the master's own cloud, so a hosted
run marks the column. It is read from the environment rather than hardcoded for
exactly the reason the example above now looks dated: the marked column moved
from `gcp` to `aws` when the master moved from Cloud Run to AgentCore. Unset —
the local mesh — every leg is loopback, the distinction does not arise, and the
table reads exactly as it always did.

Confirmed hosted on 2026-08-08, **under the Cloud Run master**, which is why the
starred column is `gcp`:

```console
client \ server  gcp*              aws               azure
a2a-sdk          ok 992ms          ok 1328ms         ok 538ms
agent-framework  ok 504ms          ok 994ms          ok 570ms
google-adk       transport         ok 5953ms         ok 471ms

8/9 attempted cells succeeded
  of which 6 crossed a cloud boundary and 2 did not
```

Six of the eight passing cells crossed a vendor boundary. The other two were
Cloud Run reaching Cloud Run, and did not count toward the interop claim. Under
the AgentCore master the same arithmetic should hold with the `aws` column
starred instead — should, because it has not been run.

Three client SDKs × three natively-served agents; see **How independent the
axes are** under Architecture for what that does and does not mean. Every cell
is one real A2A
call; a failed cell records which layer broke (`transport`, `protocol`,
`timeout`, `authentication`, `provider`) rather than just failing — the
classification walks the vendor SDK's exception chain, because every stack here
wraps the real cause in a type of its own.

## The mesh

```console
$ python3 -m coordinator.cli 100 USD EUR JPY

participants: gcp, aws, azure

100 USD = 92 EUR @ 0.92 [3/3 clouds, agreed]
    gcp                  92 (433ms)
    aws                  92 (27ms)
    azure                92 (31ms)
100 USD = 15000 JPY @ 150 [3/3 clouds, agreed]
    gcp               15000 (433ms)
    aws               15000 (27ms)
    azure             15000 (31ms)

elapsed 434ms
```

Consensus is the **median**, not a primary-plus-verifier pair, so a single
divergent cloud cannot move the agreed value once three respond — the property
that makes a third cloud worth adding. Clouds that time out or return garbage
degrade the quorum instead of failing the run.

## Architecture

```text
        coordinator/master.py  (AgentCore Runtime, us-west-2)
        itself an A2A agent -- same protocol in as out
                        |
        +---------------+---------------+
        | A2A v1.0      | A2A v1.0      | A2A v1.0
        | WIF ID token  | SigV4         | Entra token
        | keyless       | keyless       | CLIENT SECRET
        | cross-cloud   | IN-CLOUD HOP  | cross-cloud
        v               v               v
  Google Cloud      AWS              Azure
  ADK LlmAgent      Strands Agent    Agent Framework Agent
  Gemini            Bedrock Nova     Foundry model
  served by         served by        served by
  to_a2a()          a2a-sdk routes   A2AExecutor
  on               on                on
  Cloud Run        AgentCore Runtime Container Apps
  us-central1      us-west-2         westus2
```

The model row applies to `llm` mode only. The default is `direct`, where no
model is in the path at all and each agent answers from a rate provider — so
every measurement in this README other than the `llm` run is measuring vendor
*serving* stacks and the protocol, not vendor models.

Each credential starts from the master's own AWS role: a signed-but-unsent
`GetCallerIdentity` that Google's Workload Identity Federation replays to
identify the caller, plain SigV4 for the sibling AgentCore runtime, and — the
exception — an Entra client secret held in Secrets Manager for Container Apps.
Three clouds, three mechanisms, **two of them keyless**.

That last number is the whole point of the host being a decision rather than a
detail. From Cloud Run it was three of three, because Cloud Run mints workload
OIDC for an arbitrary audience and every callee here consumes external OIDC.
From AgentCore it is two, because Entra will only take a JWT and AWS will not
issue one for ordinary compute. The mesh did not change; its host did.

**How independent the axes are.** The grid reads as nine separate experiments
and it is worth being precise about that:

- **Servers: two stacks, not three.** `agents/aws/server.py` and
  `agents/azure/server.py` both build on `agents/serving.py` — same Starlette
  app, same `a2a-sdk` routes, same card builder — differing only in executor.
  Only the GCP leg, on ADK's `to_a2a()`, is a separate serving stack.
- **Clients: one transport, three façades.** `agent-framework-a2a` requires
  `a2a-sdk>=1.0.0,<2` and `google-adk` requires `a2a-sdk>=0.3.4,<2`. All three
  client stacks resolve to the same wire implementation.

So the nine cells are a presentation rather than nine independent experiments.
It is worth noting what that implies about the failures: shared implementation
on both ends might be expected to make interop straightforward, and it did not
— a platform stripped a protocol header, and a framework advertised an
unreachable address. Operationally the client side is still symmetric: any of
the three can drive the whole mesh (`--client agent-framework`).

| Layer | Module |
|---|---|
| The master, served over A2A | `coordinator/master.py` |
| N-way consensus | `coordinator/consensus.py`, `coordinator/mesh.py` |
| Cloud-agnostic participant interface | `coordinator/participants.py` |
| One credential seam, three AWS-rooted legs | `coordinator/auth.py`, `coordinator/aws_origin.py` |
| Shared prompt + reply parsing | `protocol/quotes.py` |
| Three client stacks | `clients/` |
| Three native agents | `agents/{gcp,aws,azure}/server.py` |
| Interop matrix | `matrix/` |

## Two brains

Every agent runs one of two ways, set by `CURRENCY_MODEL_MODE`:

- **`direct`** (default) — answers deterministically from a rate provider. No
  model, no credentials, no upstream. A failed matrix cell is then
  unambiguously a protocol failure, never a model that wandered off-format or
  an expired key. The vendor's *serving* stack stays in the path in both
  modes, which is what the matrix is actually testing.
- **`llm`** — the cloud's native model through its native framework: Gemini
  via ADK, Nova via Strands, a Foundry deployment via Agent Framework.
  Requires that cloud's credentials.

## On Frankfurter

The two-cloud predecessors pulled live rates from the Frankfurter API on every
leg. That is available here (`CURRENCY_RATE_PROVIDER=frankfurter`,
`--rates frankfurter`) but is **not** the default, for two reasons:

1. When every cloud reads the same upstream they agree by construction. The
   earlier run recorded the ADK agent returning `1.1367` and the MCP tool
   returning `1.1367` — a real measurement of nothing. Consensus across
   correlated sources is vacuous.
2. It folds upstream HTTP latency, rate limits, and outages into numbers meant
   to measure A2A. A red cell should never mean "Frankfurter throttled us".

Disagreement is therefore tested by **fault injection** rather than by hoping
three models diverge, at two levels: `tests/test_mesh.py` perturbs a quote
after the fact, and `CURRENCY_RATE_SCALE_<AGENT>` skews a *running* agent so
the median can be watched holding (act 4 of `./infra/demo.sh`). Live rates
remain useful as an end-to-end validation pass, which is what they are kept for.

## Setup

Requires Python 3.13 and `uv`.

```bash
uv pip install --system \
  "a2a-sdk[http-server]" google-adk \
  agent-framework-a2a agent-framework-core \
  pydantic httpx uvicorn pytest pytest-asyncio
uv pip install --system -e .
```

Latest of everything, no virtualenv — see `CLAUDE.md`. `google-adk` 2.4.0 could
not serve A2A v1.0 (finding 4 in `docs/INTEROP.md`), but that is a fact about
2.4.0: retested 2026-08-02 on `google-adk` 2.6.1 + `a2a-sdk` 1.1.2 and it
serves.

`strands-agents` is needed only for the AWS agent's `llm` mode; every other
path runs without it.

## Run

```bash
./infra/run_mesh.sh start        # three agents on :10001 :10002 :10003
./infra/run_mesh.sh status
python3 -m matrix.runner --json report.json
python3 -m coordinator.cli 100 USD EUR GBP
./infra/run_mesh.sh stop
```

## Deployed

All three agents run on their own vendor's hosting, reached from one master on a
fourth runtime. **One long-lived secret exists** — the Entra client secret on
the AWS→Azure leg — and nothing else in the running system holds one. That is a
claim about the running system, not about bootstrap: creating the pool, the app
registration and the roles used the operator's own credentials, as provisioning
always does.

Deploy each cloud with its own script, then wire the master to all three. Order
matters: the master's execution role is the principal GCP's pool trusts and the
one that reads the Azure secret, so it has to exist before those two are
configured, and `wire` re-applies its grants afterwards.

Every AWS verb preflights its credentials first: if the ambient ones do not
work it captures a fresh set with `./save-aws-creds.sh` and re-injects them,
and if that cannot help it says so and names `aws login` rather than failing
somewhere further in. It cannot refresh a dead session, and it cannot rescue a
shell whose `AWS_*` variables are wrong — environment wins the AWS credential
chain, so re-exporting returns the same broken values.

```bash
./infra/deploy_aws.sh    deploy  # the Strands agent on AgentCore Runtime
./infra/deploy_azure.sh  deploy  # Container App
./infra/deploy_gcp.sh    deploy  # the ADK agent on Cloud Run

./infra/deploy_master_aws.sh deploy   # the master, itself on AgentCore
./infra/deploy_gcp.sh   wif      # workload identity pool trusting the master's role
./infra/deploy_azure.sh secret   # Entra registration + secret -> Secrets Manager
./infra/deploy_azure.sh auth     # make the ingress demand a token

./infra/deploy_master_aws.sh wire     # fold all three legs in, refresh the grants
./infra/deploy_master_aws.sh run      # 3-cloud consensus, from the cloud
./infra/deploy_master_aws.sh matrix   # the 3x3, every client against every server
./infra/deploy_master_aws.sh verify   # the negative controls
```

The master runs *on an agent runtime* rather than locally, and that is not a
convenience: a laptop has no role the pool trusts and no path to the Azure
secret. Its host is what sets the whole auth bill — see
[`docs/DEPLOYMENT_PLAN.md`](docs/DEPLOYMENT_PLAN.md), where the same statement
was true of Cloud Run for a different reason and produced a different bill.

Being an A2A agent rather than a job means the master is reachable the same way
its peers are, so `run` is one signed JSON-RPC call rather than a job execution.
It answers the peers' own prompt template in the peers' own wire format, with
the full run envelope appended — which makes it a drop-in participant in a
larger mesh, should there ever be one.

`verify` is the part worth running twice. Every leg is probed alone, because
the mesh degrades on purpose: a three-cloud run with one credential removed
still reaches quorum on the other two and exits 0, which reads as "no denial"
and is not.

Tests are hermetic by default; the live suite skips itself unless the mesh is
up.

```bash
python3 -m pytest tests/ -q     # 120 passed, 11 skipped
```

The eleven are the live-mesh cells; with `./infra/run_mesh.sh start` running
they execute and it is 131 passed.

## Status

**Read this first.** The list below is split by which mesh it is about. Moving
the master from Cloud Run to AgentCore did not invalidate the measurements taken
under the old one, but it did make them history: they cannot be re-run from this
repo, because the apparatus that produced them was deleted with it.

### The AgentCore-hosted master — deployed 2026-08-12

- `coordinator/master.py`, the master as an A2A agent: two skills, the peers'
  prompt template in and their wire format out, with the run envelope appended.
  Reached over the same protocol it uses to reach its peers.
- Three AWS-rooted legs behind one `httpx.Auth` seam: workload identity
  federation for GCP, plain SigV4 for the sibling runtime, an Entra client
  secret out of Secrets Manager for Azure.
- **`3/3 clouds, agreed`, and consensus latency at `max(legs) + ~30ms`.** That
  is a correction, not a confirmation: the Cloud Run master's figure was
  `max(legs) + ~1s`, carefully attributed to "card fetches and credential
  mints". It was almost entirely the **job container start**. Three warm runs:
  617/623/549ms elapsed against 592/588/524ms slowest leg.
- **The AWS leg got 4× faster and the GCP leg got faster too.** AWS 274–332ms
  against 1028–1344ms — in-cloud now, and no token exchange at all. GCP
  485–494ms against 1138–1394ms, *despite* two round trips before the call where
  Cloud Run had one metadata hop: the exchange was never what dominated, the
  distance was. Azure is unchanged within noise, which is what makes the other
  two readable.
- **Hosted matrix 8/9**, the one red cell still interop finding 2 — ADK's own
  client against ADK's own server, unchanged by moving the caller to another
  cloud, which confirms the defect is in the advertised address and not in who
  is dialling.
- **Nine controls, nine for nine.** Unauthenticated `curl` gets 403 on both the
  invoke path and the agent card; each leg answers alone with its credential and
  is denied alone without it; and the right identity with the wrong audience is
  denied. Two caveats kept in the open: each probe mutates the live runtime,
  because AgentCore has no execution-time env override, and the AWS row now
  denies at AgentCore's own SigV4 requirement rather than at a cross-cloud
  federation, which is a weaker statement than it used to be.
- **Deploying found four defects a green 127-test suite did not**, three of them
  facts about platforms no local test could know: AgentCore hands its container
  only `AWS_REGION` (so credentials come from IMDS, which the resolver had
  deliberately excluded); AgentCore sessions are sticky across a deploy, so a
  pinned session ID served *the previous image* while the new one ran alongside
  it; and Google matches `Authorization` case-sensitively while httpx lowercases
  it, so a complete header list is refused as an incomplete one. See [step
  5.5](docs/DEPLOYMENT_PLAN.md#55-found-by-deploying--four-defects-a-green-suite-did-not-catch).

### The mesh itself — unaffected by the move

The master's host was the only variable. These are properties of the other
three quarters of the system and survive it intact.

- N-way median consensus with per-participant failure isolation, replacing the
  pairwise primary/verifier model.
- Three native agents, each on its own vendor's A2A serving stack.
- Three client stacks behind one interface, sharing one parser.
- The full 3×3 matrix passing locally, with two real interop defects found,
  diagnosed, and documented.
- 131 tests (120 hermetic, 11 needing the local mesh), including all nine cells
  as assertions and a cloud-goes-offline degradation case.
- One credential seam for every leg, attached to the client rather than the
  request, so the agent-card fetch is authenticated too. Peers default to
  unauthenticated, so the local matrix stays a protocol instrument. The
  mechanisms behind it changed with the host; the seam did not.

### The Cloud-Run-hosted master — measured, then retired

Everything in this section happened, on the dates given, and none of it is
reproducible from this repo any more.

- **The token-refresh branches are covered, and covering them found a latent
  crash.** A provider expiry with no UTC offset parsed cleanly and then raised
  `TypeError: can't compare offset-naive and offset-aware datetimes` on the
  *next* call, inside `usable` — a crash at a line with nothing to do with the
  cause. An unparseable one raised `ValueError`, which is not an `AdapterError`
  and so travelled back unmapped rather than as a named auth failure. Both are
  now one `_parse_expiry` helper shared by the STS and ECS paths, which had
  duplicated the same two gaps. Real AWS always sends a `Z`, so this never
  fired in production — it is a trap removed, not an outage explained.
- **All three agents are deployed on their own vendor's hosting** — Cloud Run,
  AgentCore Runtime, Container Apps — and answer one question together from a
  Cloud Run coordinator: `3/3 clouds, agreed`, and consensus latency at
  **max(legs) + ~1s** — emphatically not their sum, so the legs are concurrent,
  but not bare max(legs) either: the ~1s is the coordinator's own fixed cost,
  which no per-leg figure includes. Seven warm runs across two days now sit
  behind that: 2258–2511ms on 2026-08-07 and 1953–2169ms on 2026-08-08, with
  the gap over `max(legs)` at 880–984ms on the second day's four.
- **All three legs were keyless, and that was a measured claim rather than a
  reported one.** Seven controls, 2026-08-07: each leg answers alone with its
  credential, each is denied alone without it, and the unauthenticated `curl`
  gets 403. Each probe isolates one leg, because the median absorbs a single
  denial and exits 0 — the failure mode that would have let three decorative
  auth modes look like three working ones. The three-of-three is exactly what
  the move to AgentCore gave up; the controls themselves have been ported. See
  [`docs/DEPLOYMENT_PLAN.md`](docs/DEPLOYMENT_PLAN.md#what-the-controls-actually-proved-2026-08-07).
- **The 3×3 matrix run hosted: 8/9**, the one red cell being interop finding 2
  — still ADK's own client against ADK's own server, now with two other clouds
  beside it as controls. Of those cells, only six cross a vendor boundary: the
  coordinator and the GCP agent are both on Cloud Run, and the matrix now marks
  that column instead of letting three in-cloud hops pad the interop score.
- Deploying found what a green suite had not, twice: two defects on the first
  GCP leg (a 401 misfiled as a protocol failure, a totally failed run exiting
  0), and on Azure a leg reporting `entra-fic` in front of a public ingress.
- **AgentCore is reached under least privilege**, closing the predecessor
  series' longest-standing open question. Its finding — that only
  `Resource: "*"` permitted the agent-card fetch — was a misdiagnosis: the card
  fetch is a separate IAM action (`bedrock-agentcore:GetAgentCard`), not a
  resource-scope problem. Confirmed by removing that one action and nothing
  else, which breaks discovery while the invoke keeps working. **AWS had been
  naming the missing action in the response body all along**; the earlier
  adapter kept the status code and threw the body away. That is why
  `coordinator/auth.py` logs the raw provider response at every auth boundary,
  and it is the first time that decision has paid for itself.

### Not done

- **The AgentCore master's numbers are one warm session.** Three consecutive
  runs, one cold, all on 2026-08-12 — against seven warm runs across two days
  for the Cloud Run master. The `+25/+35/+25ms` over `max(legs)` is tight enough
  to be worth quoting and thin enough that it should not be quoted as a general
  property of agent runtimes.
- **Roughly 32 tests are gone.** The 2026-08-02 session left 92 passing; this
  repo has 60, because that work was recovered from a Cloud Build tarball and
  `.gcloudignore` excludes `tests/`. What they covered is unknown. The
  surviving 71 (60 + 11 skipped) passed; the suite is now 107 (96 + 11)
  after the in-cloud-hop and token-lifecycle work added twenty-five.
- **Nothing outstanding on AWS scoping** — this one moved to the done list:
  the deployed policy is scoped to one runtime ARN, `Resource: "*"` is not
  required, and the predecessor's contrary finding was a misdiagnosis. See
  below.
- ~~**Most hosted latencies are single runs.**~~ Done 2026-08-08: the matrix
  has five consecutive warm runs behind it and the consensus seven across two
  days, all labelled warm or cold. What that bought was not a tighter number
  but a **retracted one** — see the AWS session finding below. Cold remains a
  separate regime, not averaged in: the Azure leg measured 23378ms cold against
  441–570ms warm in the same session.
- **A documented anomaly turned out to be an artefact of the instrument.** The
  "`agent-framework` → AWS is reproducibly ~5.7s, unexplained" claim is
  withdrawn. Each matrix cell minted a fresh AgentCore session id
  (`coordinator/auth.py:741`), each session gets its own microVM, and the ~5.9s
  was that microVM starting — landing on whichever cell drew cold capacity,
  which is why the slow cell *moved between clients* across runs. Pinning
  `AWS_A2A_SESSION_ID` removes it (5926–6037ms → 704/710ms) and unpinning
  brings it back, interleaved so it is not warming drift. The mesh mints a
  fresh session per run too, so a consensus run can pay this without warning.
  See [`docs/INTEROP.md`](docs/INTEROP.md).
- **The AWS STS and Entra paths are proven end to end, but only on the happy
  path plus one denial each.** No token has ever expired in production: every
  deployed run is a Cloud Run job that lives a few seconds, so the refresh
  branches never execute there. They are now covered hermetically instead —
  the skew window, STS credentials that expire mid-run or arrive already
  expired, a short-lived Entra token, and an unreadable `exp` — because expiry
  is a clock question rather than a network one. What is still untested is a
  *real* aged token from either provider, and genuine clock skew between the
  coordinator's clock and theirs.
- **`llm` mode runs on all three clouds, deployed** (2026-08-09), having never
  produced an answer anywhere before. A hosted 8/9 with `brain=llm`: Gemini via
  ADK over MCP on Cloud Run, Nova via Strands on AgentCore, gpt-5-mini via
  Agent Framework on Container Apps. Nova was fastest at 2.1–2.5s against
  4.4–12.3s for the other two — but they are different model classes in
  different regions on one run each, so that ordering is an observation, not a
  benchmark. Most of what it took to get there was packaging and configuration;
  see [`docs/INTEROP.md`](docs/INTEROP.md), and note that **AgentCore drops the
  `A2A-Version` header** while the other two forward it — a mechanism the
  predecessor series had already identified, reproduced here with controls.
- **`llm` numbers are one run each.** The all-three-`llm` matrix has a single
  execution behind it, unlike the direct-brain figures, which have five. Model
  latency is also far noisier than protocol latency, so those cells order the
  brains loosely at best.
- **`direct` is the steady state and the mesh is back on it.** `llm` mode was
  proved and then stood down: the matrix is a protocol instrument first, and a
  model in the path turns a red cell into two possible explanations. All three
  deploy scripts default to `direct` and take `MODEL_MODE=llm` to opt in.
- ~~**The matrix's `brain=` label reads the wrong environment.**~~ Fixed
  2026-08-09. Every agent reports its own brain on `/health`, and the matrix
  asks each server rather than reading its own `CURRENCY_MODEL_MODE`. A mixed
  mesh is reported as one: `brain=mixed (gcp=direct, aws=unknown, azure=llm)`.
  The probe carries the peer's credential, because `/health` sits behind the
  same privileged ingress as everything else. **AWS reads `unknown`** — its
  endpoint ends in `/invocations/`, so there is no `/health` to fetch — and
  saying so beats the confidently wrong value it replaced.
- No token or cost accounting. Warm/cold is now labelled everywhere it is
  recorded, but only the consensus run has more than one sample behind it.
