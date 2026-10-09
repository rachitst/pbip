# Security policy

PBIP is an academic simulation; it holds no real customer data or money. Treat it as if it did.

* Report a vulnerability privately to the repository owners (GitHub *Security advisories* -> "Report a vulnerability"),
  not in a public issue. Include steps to reproduce and impact.
* Never commit secrets. If one leaks: rotate it immediately, then remove it from history.
* Threat model and controls: `docs/security/threat-model.md`. Engineering rules: `AGENTS.md` section 5.
