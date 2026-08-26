# ADR-018: The cockpit renders server truth; it never manufactures liveness

1. **The Investigation Ledger is a pure function of the audit chain.** Every
   row maps 1:1 from a hash-chained audit step. The UI has no event
   vocabulary of its own, so it cannot show work that didn't happen.
   "Liveness" = staggered reveal over real entries + a 4s state poll.
2. **The countdown ticks locally; the server owns time.** Snapshot from
   /deadline, local ticks off performance.now(), 30s re-sync, thresholds
   mirror the server's (test-asserted). EXPIRED disables buttons as UX —
   the server rejects expired mutations regardless (R4). No client-side
   timer persistence → refresh-consistent by construction.
3. **Provenance badges ride wherever a fact appears** (header, ledger,
   exhibits, KB popovers).

Voice = built-in SpeechRecognition, feature-detected, typed-first. No CDNs,
fonts, or JS deps; offline-capable; prefers-reduced-motion honored; all
user strings pass one esc() choke point.

Rejected: SSE/WebSocket streaming (orchestrator is synchronous — polling
the audit chain is the same truth, fewer failure modes); localStorage
timers (drift for no gain).
