# Compatibility

Agent Runtime separates the **supported deployment contract** from the operating-system versions that are **continuously qualified by the manual release gate**.

## Supported contract

- Architecture: **Apple Silicon / arm64**.
- Minimum supported macOS contract: **macOS 13**.
- Intel/x86_64 Macs are unsupported.
- The minimum contract is declared by both `macos/Package.swift` and `macos/AppBundle/Info.plist`.
- Runtime source version remains **0.5.1** in this task.
- The latest public release remains **v0.5.1**. This task does not publish v0.5.2.
- Current published compatibility contract:
  - substrate manifest schema = **2**
  - candidate handoff schema = **2**
  - payload schema = **1**
  - zero-cost transaction schema = **6**

The release-bundle manifest records the schema values carried by the exact frozen candidate being qualified. That keeps candidate verification truthful if source packaging evolves before a later public release; it does not retroactively change the published v0.5.1 contract.

## Continuously release-gate qualified

The manual `.github/workflows/release-gate.yml` matrix qualifies ARM64 release artifacts on:

- macOS 14
- macOS 15
- macOS 26

macOS 13 remains part of the supported deployment contract, but it is **not continuously CI-qualified** by the current standard release-gate matrix.

The release gate is `workflow_dispatch` only. It verifies the frozen release artifact without installing or cutting over Runtime, launching the menu-bar app, requiring Runtime API credentials or Tunnel ID, creating a tag, or publishing a GitHub Release.

## Release reproducibility boundary

The reproducibility contract is intentionally narrower than compiler reproducibility:

```text
same frozen zero-cost candidate
    ->
identical release archive
identical release-manifest.json
identical SHA256SUMS.txt
```

The candidate closure, external handoff, initial payload closure, source revision/tree and requirements-lock identity remain the build identity. No claim is made that two independent Swift/compiler builds are byte-for-byte identical.
