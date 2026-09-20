# Eason One Product Checkpoint — Live Employee Provider Routing V2

Status: FIRST USABLE TRIAL HOTFIX SOURCE

- Persistent Employee identity is no longer equivalent to one persistent provider binding.
- Formal Company runtime centrally replaces mock, Codex-on-non-Engineer, unconfigured, inactive, archived, or unpriced bindings with a configured real provider.
- Current configured OpenAI / Anthropic providers are sufficient; Gemini / Perplexity are optional candidates only when their credentials/configuration exist.
- Research remains stricter: live web research requires a configured/priced OpenAI Web Search or Perplexity model.
- HR assessment reserve/pricing uses the same effective real-provider policy instead of requiring HR's persistent binding itself to be real.
- Provider max-output differences are normalized at the central execution boundary.
- The installer smoke must run with TESTING=false so it validates formal routing rather than intentionally-permitted mock test routing.
