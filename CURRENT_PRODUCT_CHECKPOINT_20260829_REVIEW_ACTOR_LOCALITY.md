# Eason One Product Checkpoint — Review Actor + Research Locality

Date: 2026-08-29
Install state: DO NOT INSTALL unless ChatGPT explicitly says `現在請更新`.

## Release target

First Usable Multi-Employee Project Trial.

## Meaningful chunk

1. Founder approval handoff stays aligned with Company Runtime: newly approved v0.20 Projects enter the Project/company surface rather than leaving Founder on the legacy Mission surface.
2. OpenAI hosted Web Search now supplies an explicit approximate search locality, defaulting to country `TW` and timezone `Asia/Taipei`, with environment overrides. The actual locality is retained in provider tool metadata.
3. Parallel-wave scheduling now counts the Employee who will perform the next action. READY/EXECUTING Work uses the accountable assignee; VERIFYING Work uses the frozen independent reviewer. This prevents one Critic/Reviewer from being scheduled for two simultaneous accountable actions merely because the underlying Works have different producers.

## Why this matters for the first usable trial

The release path now keeps Founder navigation aligned with Company Truth, local research from silently inheriting US ranking context, and parallel review from violating Persistent Employee workload semantics.

## Validation available in this environment

- Python compileall: PASS
- headquarters.js syntax check: PASS
- Full Flask/SQLAlchemy runtime acceptance: NOT RUN in this Linux tool environment because Flask / flask_sqlalchemy are unavailable.

## Next release-path focus

Continue from provider execution -> submitted ArtifactVersion -> independent review -> accepted Artifact handoff -> Project outcome review / continuation -> Result Ready. Only release-blocking current-path defects should stop the march toward the first Windows Project trial.
