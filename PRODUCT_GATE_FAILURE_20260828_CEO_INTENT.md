# Product Gate Failure — Founder work request misrouted as budget/status brief

Observed on Windows Founder surface after r4 boot.

Founder request asked Eason One to add `GET /api/founder-ping`, required real loopback HTTP verification, and supplied a Project budget of NT$5.

Observed behavior: CEO returned only company spending/remaining authority and did not create a governed Project proposal.

Root cause: Chinese work verb `新增` was absent from both the shared governed-work classifier and the operation kernel work terms. The same request contained `預算`, so the router selected the deterministic/read-only budget/status path instead of governed execution.

Correction: Founder work intent classification now recognizes `新增`, `加入`, and `加上`, and `operation_kernel.route_command()` consults the shared `requests_governed_work()` classifier before status-only routing. A focused Governance acceptance reproduces the exact Founder request and requires `ACT / AUTO_DELEGATION`.
