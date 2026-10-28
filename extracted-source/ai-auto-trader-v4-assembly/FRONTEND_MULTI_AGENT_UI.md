# Frontend milestone: multi-agent navigation and safety screens

This milestone changes only Android frontend code. The Python backend, backend configuration, risk engine, market worker and execution code are preserved from `ai-auto-trader-v4-master-assembly.zip`.

Added Android navigation and screens:
- AI Agents: intended orchestrator topology, role descriptions, status labels, communication contract.
- Positions: explicit empty state when paper ledger is unavailable; no fabricated positions.
- Risk Center: fail-closed safety indicators and disabled emergency-stop control while backend is disconnected.
- Markets navigation opens the existing Charts & Analysis screen.

No backend connectivity is claimed. GAT/PPO agent execution and live API status are not implemented by these UI screens. Exchange order placement remains disabled. UI labels explicitly identify disconnected or planned components.

Build verification: not run in this environment because neither `gradle` nor a Gradle wrapper is available. Run the repository Android APK workflow to compile and validate on GitHub Actions.
