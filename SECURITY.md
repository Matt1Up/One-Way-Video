# Security policy

## What counts as a security issue here

The threat this project cares about is a **fabricated or altered recording
passing verification**. Reports in that category get priority:

- A way to modify a bundle, frame, receipt or the video such that the checks in
  `verify/` still report a pass.
- A way to produce receipts (OpenTimestamps or Roughtime) that the pipeline
  accepts but that do not actually bind the data or the time they claim.
- A flaw in the on-screen hash / OCR loop that lets the displayed chain diverge
  from the recorded one without the reports noticing.
- Anything in the capture stage that would let credentials, keys or private
  files leak into a session's published artifacts.

Ordinary bugs (a script crashes, a report column is blank) are welcome as normal
GitHub issues.

## Reporting

Email **Matt@MnCourtFraud.com** with the subject line `One-Way-Video security`.
Include what you found, how to reproduce it, and what you think it lets an
attacker do. You will get an acknowledgement within a week. If the report is
valid, the fix, a test that covers it, and a note in `verify/README.md`
describing the previous weakness will ship together, and you will be credited
unless you ask not to be.

There is no bug bounty. This is one person's open-source project.

## Scope notes

- The verification pipeline intentionally trusts two external services for
  time: OpenTimestamps calendar servers (and `blockstream.info` for block
  lookups) and `roughtime.cloudflare.com`. Compromise of those services is out
  of scope here and is documented as a limitation in `verify/README.md`, which
  also explains how to verify against your own Bitcoin node instead.
- The capture stage intercepts TLS with mitmproxy **on the operator's own
  machine, for the operator's own browsing session**. It is not designed to be
  pointed at other people's traffic and is not hardened for that.
- Published sessions are public by design. Anything an operator chooses to
  capture and publish is their responsibility to review first.
