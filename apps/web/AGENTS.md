<!-- BEGIN:nextjs-agent-rules -->

# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` (resolved from this file's directory; in monorepos the `next` package may not be visible from the repo root) before writing any code. Heed deprecation notices.

This block is written and re-added by `next dev` — verify at `node_modules/next/dist/server/lib/generate-agent-files.js`. Removing it from a diff only re-creates the uncommitted change; committing it with your work keeps the tree clean.

<!-- END:nextjs-agent-rules -->

# ultrademo web

- The demo player lives under `src/app/d/[slug]`. Its realtime logic is `src/lib/session.ts`, a plain
  class around `livekit-client`'s `Room`; React only renders its snapshot.
- Wire names (topics, attributes, RPC methods, event types) mirror
  `packages/protocol/src/ultrademo_protocol/envelope.py`. Change both together.
- LiveKit APIs: check the installed type definitions under `node_modules/livekit-client/dist/src`
  and `node_modules/@livekit/components-react/dist` before using one.
