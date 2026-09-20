# Eason One Governance Working Status — r7

Engineering snapshot: `governance-r7-20260828`

Current live Product Gate chain already demonstrated on Project #20:
- Founder Project proposal and exact NT$5 Contract creation worked.
- Restart preserved durable Project/Work/Governance state.
- r6 repaired host-verifier -> semantic-reviewer evidence handoff; previously successful code + real loopback HTTP evidence can now produce ACCEPTED Work instead of false rejection loops.
- r6 also allowed the known `SYSTEM_RECOVERY` protocol defect to unlock once and resume without Founder intervention.
- A real Project budget extension gate appeared only when the next paid closure step exceeded the remaining Founder authority.

New r7 blocker fixed from live Product Gate:
- After Founder REJECTED that budget amendment, r6 reopened another budget gate on the next scheduler pass.
- r7 makes rejection durable negative authority under unchanged governing Contract/budget authority and blocks automated repeat asks.
- Explicit Founder-initiated reconsideration remains possible.

Local source checks available in the Linux delivery sandbox:
- Python compileall: PASS.
- v0.20 Core structural audit: PASS.
- Governance structural audit: 71/71 PASS.
- Flask/SQLAlchemy focused ORM diagnostics cannot run in this Linux delivery sandbox because those Python dependencies are absent here; the Windows installer/verify remains mandatory and fail-closed.

Product Gate is not yet complete. Project #20 is expected to become honestly BLOCKED inside the rejected NT$5 cap unless existing deterministic Project evidence is sufficient for no-cost closure. It must not reopen the same budget request automatically.
