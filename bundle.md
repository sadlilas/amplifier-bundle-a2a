---
bundle:
  name: a2a
  version: 0.1.0
  description: |
    Agent-to-Agent communication via the A2A protocol.
    Enables Amplifier sessions on different devices to communicate
    over a network (LAN, Tailscale, internet).

includes:
  - bundle: a2a:behaviors/a2a.yaml
---

# A2A Bundle

@a2a:context/a2a-instructions.md
