# Eason One

The first usable vertical slice of a personal AI-native company operating system. It provides a persistent CEO, Employees, governed Projects and Tasks, explicit model executions, reviews, Founder interviews, Company Brain proposals, and an auditable NT$3,000 real-cost budget.

## Run

```powershell
python -m pip install -e ".[test]"
flask --app run.py seed
flask --app run.py run
```

Open `http://127.0.0.1:5000`. The default database is `instance/eason_one.db`.

The seeded team uses the zero-cost `MockProvider`, so the complete flow works without credentials. To configure a real provider, create an active `ModelConfig` with `provider_key="openai"`, its exact `model_name`, positive input/output prices in TWD, and a maximum output-token limit; then assign it through the employee service and set `OPENAI_API_KEY`. Real-provider zero pricing, currency mismatches, and inactive configurations are rejected before invocation. OpenAI Responses retain both the response ID and HTTP request ID for audit. Environment variables hold secrets only and cannot override the Employee's organizational model assignment.

## Test

```powershell
pytest -q
```
