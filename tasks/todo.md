# Plugin extension API execution checklist

Design, decisions, detailed acceptance criteria, verification, dependencies, and file scopes are in [PLAN.md](../PLAN.md). This is the execution status target. Tasks 01–14 and 17–18 are implemented and verified offline where noted; Task 15 remote hosting and Task 16 remaining deferred.

## Commands

- [x] [Task 01: Define the minimal extension contracts](../PLAN.md#task-01-define-the-minimal-extension-contracts)
- [x] [Task 02: Bind commands to the native plugin composition](../PLAN.md#task-02-bind-commands-to-the-native-plugin-composition)
- [x] [Task 03: Invoke a user-only command through the client SDK](../PLAN.md#task-03-invoke-a-user-only-command-through-the-client-sdk)
- [x] Checkpoint A: Offline direct command works through ACP; D1 reviewed; static checks pass.
- [x] [Task 04: Surface namespaced commands with inline fallback output](../PLAN.md#task-04-surface-namespaced-commands-with-inline-fallback-output)
- [x] [Task 05: Bound command execution and cancellation](../PLAN.md#task-05-bound-command-execution-and-cancellation)
- [x] [Task 06: Execute explicit StartTurn results](../PLAN.md#task-06-execute-explicit-startturn-results)
- [x] Checkpoint B: User-only readout and explicit normal turn work; no model-history pollution or plugin-specific TUI branches.

## Tool presentation

- [x] [Task 07: Forward tool presentation through one native observer](../PLAN.md#task-07-forward-tool-presentation-through-one-native-observer)
- [x] [Task 08: Merge supplemental presentation into standard tool state](../PLAN.md#task-08-merge-supplemental-presentation-into-standard-tool-state)
- [x] [Task 09: Restore final tool presentation passively](../PLAN.md#task-09-restore-final-tool-presentation-passively)
- [x] Checkpoint C: D2 proved using public APIs; model behavior unchanged; final snapshots replay without actions or extra persistence machinery.

## Textual UI

- [x] [Task 10: Define local Textual adapter discovery](../PLAN.md#task-10-define-local-textual-adapter-discovery)
- [x] [Task 11: Mount custom command-result views](../PLAN.md#task-11-mount-custom-command-result-views)
- [x] [Task 12: Mount custom tool input/output views](../PLAN.md#task-12-mount-custom-tool-inputoutput-views)
- [x] Checkpoint D: Independent external command/tool widgets work; native Textual lifecycle; runtime/protocol import boundaries hold.
- [x] [Task 13: Route widget interactions through plugin commands](../PLAN.md#task-13-route-widget-interactions-through-plugin-commands)
- [x] [Task 14: Finish widget retention and failure boundaries](../PLAN.md#task-14-finish-widget-retention-and-failure-boundaries)
- [ ] [Task 15: Verify stdio, remote, and headless behavior](../PLAN.md#task-15-verify-stdio-remote-and-headless-behavior)
- [ ] Checkpoint E: Interactive external plugin works over both transports; offline evidence recorded; D3 scope reviewed.

## Conditional upstream work

- [ ] [Task 16: Add conversation writes only after a public upstream operation exists](../PLAN.md#task-16-add-conversation-writes-only-after-a-public-upstream-operation-exists)

Task 16 is unavailable on the inspected dependency pins. Leave it unchecked and record it as **deferred/unsupported** unless its upstream prerequisite and dependency decision are satisfied. Do not mark it complete because other extension work ships. Supported commands can ship independently, with this limitation reported explicitly.

## Example, documentation, and final checks

- [x] [Task 17: Publish a small installable example plugin](../PLAN.md#task-17-publish-a-small-installable-example-plugin)
- [x] [Task 18: Document the extension authoring API](../PLAN.md#task-18-document-the-extension-authoring-api)
- [ ] [Task 19: Run final gates and report supported scope](../PLAN.md#task-19-run-final-gates-and-report-supported-scope)
- [ ] Checkpoint F: Mandatory acceptance criteria met; external example verified; conditional limitations explicit; result reviewed by the user.

## Implementation review

- [ ] User has reviewed and approved the implementation plan, including the supported scope and D1–D3 boundaries.

Task 16 remains deferred/unsupported on the pinned ACP adapter (no public writable-history operation). Task 15 still needs the WebSocket hosting walkthrough; Task 19 is partially complete (stdio/local gates and offline evidence recorded).
