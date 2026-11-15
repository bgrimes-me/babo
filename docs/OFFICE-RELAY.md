# The office relay — the pod's only window

> "The relay is our window into the pod. It's an API surface. The relay is the only
> thing that can see the agent directly." — Evans

Written before any code, because the contract is the thing that has to be agreed and
because a contract is also what makes the relay testable **without a pod**: the
handlers take a transport, so the same code that serves a socket can be driven by a
test that never opens one.

## What it is

One process in the agent's own image, first up, before the agent. It is the only
component positioned to see the agent locally, so it is the only one that can answer
for it; nothing outside the pod talks to the agent at all.

- **It reports. It does not decide.** No authority, no policy, no fallback. The
  Manager decides; the relay witnesses.
- **Every answer it cannot substantiate is `unknown`**, never a considered `fine`.
- Ingress is the relay's. The agent's own services bind loopback; the pod still has
  its own network for egress (Forgejo, at minimum).

## Why Python, not the Ruby relay that exists

`neoclaw-rails/app/services/hub/relay.rb` is the existing sidecar (`<wg_address>:9300`,
`POST /message`, `GET /health`). It is not being translated; it is being **extended**,
and it is being written in the language of the thing it watches.

1. It must be in the same image as the agent. A sidecar in a second runtime is more
   to go wrong and more to patch, on every pod, forever.
2. Its job is thin by design. Ruby would buy a language and a CVE surface for a
   component that must never decide anything.
3. The Ruby API is a **subset**: `health` and `message` exist as concepts; `status`,
   `quiesce` and `flush` are new on top. Nothing is being re-invented.

## The surface — five calls, closed

An open-ended surface becomes four. These are the five, and a request for a sixth is
a design conversation, not a patch.

All responses are `application/json`. Every failure that is not the relay's own
fault uses the house shape `{"detail": "<sentence>"}`.

## One failure shape, five call sites

Every call that cannot substantiate an answer returns **the same shape**:

```
200 {"unknown": "<call>: <the reason, in the house's words>"}
```

- one key, `unknown`, always a string, always prefixed with the call it belongs to;
- **the endpoint's own success fields are ABSENT, never `false`** — `seen: false`,
  `accepted: false`, `persisted: false`, `delivered: false` are all *claims*, and a
  relay that cannot see has no claim to make;
- no per-endpoint dialect. A consumer parses one shape for five call sites, so
  "I could not see" cannot be mistaken for "fine" by the one case somebody forgot.

`health` never carries `unknown`: if the relay answers at all, the relay is up. That
is the one thing it can know without asking anybody.

## The status code is a claim about the transport

A relay that returned 500 because the agent did not answer would be **reporting its
own health as the agent's**. The code belongs to this hop; the answer belongs to the
thing it asked. Same error as `delivered` on a 404, or `status: ok` over an error.

### `GET /health`

Is the **relay** up. It makes **no claim about the agent** — that is `status`'s job,
and conflating the two is how a dead agent gets a green.

```
200 {"ok": true, "relay": {"version": "1", "uptime_s": 42}}
```

A Manager probing this and getting an answer can say *the relay is up*. It may not
say *the agent is well*; for that it asks `status`.

### `GET /status`

Where the agent's turn is, **asked locally**, and `unknown` when the relay cannot see
it. Never `idle` by default: silence must not become "nothing is happening".

```
200 {"seen": true, "in_flight": true,
     "turn_id": "t-9f21", "wall_started_at": "2026-11-14T22:10:00Z"}

200 {"seen": true, "in_flight": false,
     "turn_id": null, "wall_started_at": null}

200 {"unknown": "status: the agent did not answer its local door (connection refused)"}
```

A **200 with `unknown`** — the relay is fine and the agent is not answering, and the
absence of `seen`/`in_flight` is the fact. A relay that turned that into a 500 would
report its own health as the agent's.

### `POST /quiesce`

Stop admitting new work and return where the turn stands. **Quiesce, not kill**:
a turn in flight is allowed to reach a boundary; the pod is not torn down here.

```
POST {"reason": "sessions leaked"}
200  {"accepted": true, "in_flight": true, "turn_id": "t-9f21",
      "wall_started_at": "2026-11-14T22:10:00Z"}

200  {"accepted": true, "in_flight": false}

200  {"unknown": "quiesce: the agent did not answer its local door (timeout after 5.0s)"}
```

An `unknown` answer means *the request was not taken*, not *the agent refused*. A
refusal by the agent is a `200` with the agent's own sentence in `detail`, because the
relay does not reword what it is told — a refusal is an answer, and `unknown` is not.

### `POST /flush`

Persist and confirm. It must not claim a persist it cannot substantiate.

```
POST {"reason": "quiesced for restart"}
200  {"persisted": true,  "detail": "the agent confirmed its store was written"}
200  {"unknown": "flush: the agent did not confirm, so the store state is unknown"}
```

### `POST /message`

Message delivery — the door the notification design already gave the relay. Shape
kept from the Ruby relay so a caller does not learn two vocabularies.

```
POST {"from": "@evans:matrix.home", "channel": "matrix", "content": "hello",
      "attachments": []}
200  {"delivered": true}
200  {"unknown": "message: the agent's local door did not accept it"}
```

## What it must never do

- **Decide.** No admission policy, no allow-list, no retry that turns "no answer" into
  "delivered". Delivery is the only thing a caller is told, and only when it happened.
- **Speak for the agent.** Any sentence attributed to the agent is quoted; any answer
  the relay could not obtain is `unknown` with the reason it could not.
- **Listen anywhere the agent can be reached without the relay.** The relay's own bind
  is its decision; the agent's is not a decision at all (loopback).

## How it is tested without a pod

The handlers take the agent's local door as an injected transport, exactly as
`HopClient` takes one. So the same functions that serve a socket are driven in tests
with a fake door that can answer, refuse, time out or return nonsense — and the
contract above becomes rows that run in CI, on a runner, with no container.

That is deliberate: this file exists so the relay does not become another row that
can never run.
