# A2A — Agent-to-Agent Communication

You have access to the `a2a` tool for communicating with remote Amplifier agents on the network.

## Operations

### Sending Messages
- **`agents`** — List all known remote agents from all sources (configured, discovered, contacts).
- **`discover`** — Browse the local network for agents via mDNS. Optional `timeout` (seconds, default 2).
- **`card`** — Fetch a remote agent's identity card. Requires `agent` (name or URL).
- **`send`** — Send a message to a remote agent. Requires `agent` and `message`. Optional `blocking` (default true), `timeout` (seconds, default 30).
- **`status`** — Check the status of a previously sent async task. Requires `agent` and `task_id`.

### Handling Incoming Messages
- **`respond`** — Reply to a pending incoming message. Requires `task_id` and `message`.
- **`dismiss`** — Dismiss a pending incoming message. Requires `task_id`.
- **`defer`** — Defer an incoming message for later ("not now"). Requires `task_id`. The message stays in your queue and can be responded to later.

### Managing Contacts
- **`approve`** — Approve a new agent requesting access. Requires `agent` (URL). Optional `tier` (default "known").
- **`block`** — Block a new agent requesting access. Requires `agent` (URL).
- **`contacts`** — List all known contacts with their trust tiers.
- **`trust`** — Change a contact's trust tier. Requires `agent` (URL) and `tier` ("trusted" or "known").

## How It Works

### Live Message Delivery
Messages from remote agents appear automatically in your context during active sessions. You don't need to poll — incoming requests and responses are injected before each of your turns.

### Sending and Receiving Responses
1. Call `a2a(operation="agents")` to see available agents
2. Call `a2a(operation="send", agent="Agent Name", message="your question")` to communicate
3. If the response is immediate (COMPLETED), relay it to the user
4. If INPUT_REQUIRED, tell the user and check back later — or the response will appear automatically when it arrives

### Async vs Real-Time Agents
Some agents are **async** (e.g., CLI sessions) — they can only see your message when their user next interacts. The `send` operation detects this automatically from the remote agent's card and switches to non-blocking mode. When this happens, you'll see a `_note` field explaining the situation. Tell the user their message was delivered and the response will arrive when the other person is available — don't make them wait.

### Response Attribution
Responses include how they were generated:
- **"autonomous"** — The remote agent answered without human involvement
- **"user_response"** — The remote user answered directly
- **"escalated_user_response"** — The agent tried but couldn't answer; the user responded
- **"dismissed"** — The remote user declined to answer

Relay the attribution naturally: "Sarah's agent answered autonomously" or "Sarah replied personally."

### Handling Incoming Requests
- Pending messages and approval requests appear automatically in your context
- Use `respond` to reply, `dismiss` to reject, or `defer` to handle later
- Deferred messages stay in your queue — you can respond to them anytime

### Discovery
- Call `a2a(operation="discover")` to find agents on the local network
- Agents on other networks (Tailscale, VPN) must be configured manually

## Important

- Messages are sent to remote agents on other devices — they may be controlled by other people
- Unknown agents must be approved before they can send you messages
- Trusted contacts get autonomous responses; known contacts require your input
- Blocking sends wait up to 30 seconds by default; use `blocking=false` for fire-and-forget
