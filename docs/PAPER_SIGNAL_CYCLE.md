# Validated signal → paper intent cycle

`app.engines.paper_signal_cycle.PaperSignalCycle` connects an already validated
`SignalHandoffResult` to `SignalWatcher` and `TradingOrchestrator`. It is a
paper-only integration seam, not a raw-market-to-trade strategy engine.

## Safety sequence

1. Reject invalid or authorization-claiming handoffs.
2. Check symbol agreement across signal, watcher request, proposal, and tick.
3. Require proposal entry/stop/target and side to agree with the validated signal.
4. Register a watch once; reject conflicting reuse of a watch ID.
5. Evaluate a fresh tick using the watcher. A waiting/invalidated/expired watch
   cannot record an intent.
6. Refresh the market snapshot from the tick and pass the watch result and
   validated signal to the deterministic risk guardian through the orchestrator.
7. Record a paper intent only if validation, trigger, risk, and ledger checks pass.

All returned results keep `orders_authorized=False`. The coordinator has no
exchange client and does not submit live or Testnet orders.

## Integration boundary / known limitation

This component expects an upstream research candidate, explicit price plan,
validated handoff, and a correctly sized `OrchestrationRequest`. The next step
is a service-level composition that obtains and validates those inputs from the
market worker and research/price-planning stages, then persists paper fills and
protection updates. Until that full service is verified, do not describe the
project as autonomous end-to-end trading.
