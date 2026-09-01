# Spencer Revoy

I build software that has to keep its promises: agent systems with real authority
boundaries, media tools that survive long-running operation, and automation whose
state can be inspected when something goes wrong.

My usual stack is Rust, Python, TypeScript, and Linux. I design and build
greenfield systems end to end, from the first architecture and working product
through deployment and operation. The through-line is clear contracts, durable
state, tests around the dangerous edges, and a human decision before an
irreversible action.

The subject matter varies, but the build pattern is consistent: start with an
ambiguous problem and a blank repository, find the architecture, ship the first
complete vertical slice, and keep going through deployment, observability, and
recovery.

## Work you can inspect

- [MCL](https://github.com/revoydotdev/mcl) is a Rust runtime and declarative language
  that keeps application state outside the LLM, validates its signals, and enforces
  declared knowledge boundaries.
- [Astrolabe](https://github.com/revoydotdev/astrolabe) turns authenticated web
  behavior into versioned API contracts and governed MCP toolpacks.
- [Strivo](https://github.com/revoydotdev/strivo) is a self-hosted live-stream PVR with
  a daemon, local web UI, recovery tooling, and an optional Creator Edition.
- [mcp-safety-core](https://github.com/revoydotdev/mcp-safety-core) packages the
  preview/confirm gates and structured failures I reuse across MCP servers.
- [Rylus](https://github.com/revoydotdev/rylus) is a security-focused modernization
  of Weylus for low-latency screen and input streaming.
- [Weave](https://github.com/revoydotdev/weave) is a greenfield multi-model video
  workspace spanning native iOS and web clients.

There is more public work at [RevoyDotDev](https://github.com/revoydotdev), from
audio analysis and live visualization to research tools and small Linux utilities.
Some of my largest systems remain private because they contain client,
infrastructure, or third-party operational material; I describe those without
pretending the source is available for review.

## Current activity

![Profile](card.png?v=1788264114)
