# EASON ONE Demo Guide

這份流程用 current repository 的 Founder-facing UI 與 durable data 展示 EASON ONE；不需要為了 demo 觸發付費 provider，也不把 engineering prototype 描述成 production-ready 產品。

## 啟動

Windows PowerShell：

```powershell
.\scripts\run-headquarters.ps1
```

如果是全新資料庫，先執行：

```powershell
flask --app run.py seed
```

開啟：

```text
http://127.0.0.1:5000/headquarters
```

## 3–5 分鐘 Demo

1. **Headquarters：公司現在真的在做什麼**  
   先看 Active Projects、AI Employees、Founder attention、cost 與 recent activity。第一屏只講 current company truth，不先進 technical audit。

2. **Project：一個目標如何被組織推進**  
   打開一個 current Project，展示 objective、canonical state、semantic progress、Project Team、blocker、next step、Founder action 與 budget。

3. **Project Team：誰現在在做什麼**  
   指出 Employee 的 `WORKING / WAITING / UP NEXT / QUEUED`。同一 Employee 同時間只服務一個 Active Project；同一 Project 內可以排多個 Work，但會依序執行。

4. **Execution → Artifact → Verification → Result**  
   說明 EASON ONE 不把模型「說完成」當成完成；需要真正 Execution、可追蹤 Artifact 與 Verification evidence。

5. **Failure / Recovery**  
   WAITING 可以是正確的 quiescent state。Missing evidence、超權限或 ambiguous side effect 時，系統應停止、reconcile 或等待正確 authority，而不是無限 retry 或製造 busywork。

6. **Technical audit（教授想深入時再展開）**  
   System / audit 可以看到 AgentRun、provider、receipt、retry lineage、hash 與其他工程證據，但這不是 Founder 頁面的第一層資訊。

## Project #22（若 current database 仍存在）

Project #22 可以作為 missing-evidence 範例：Founder-facing state 應是 `WAITING`，而不是因為 management Work 還在 ownership envelope 就誤顯示成 `WORKING`。

目前介面會把 Researcher / Critic / Product Strategist 的責任與順序投影成 WAITING / UP NEXT / QUEUED。重新整理頁面不應因此產生新的 provider call、成本或外部副作用。

如果 current database 已沒有 Project #22，直接使用任何具有相同 durable semantics 的 Project，不要為了 demo 修改真實狀態。

## 教授如果只看三個地方

1. `README.md` + `docs/ARCHITECTURE.md`：產品問題與完整資料流。
2. `eason_one/services/company_kernel.py`、`work_runtime.py`、`governance.py`、`project_outcome.py`：Work、capacity、authority、Result truth。
3. `tests/test_ceo_operating_system.py`、`tests/test_v020_governance_floor.py`、`tests/test_v020_current_regressions.py`：quiescence、governance、recovery / replay regression。

## Demo 邊界

- EASON ONE 是 **Active development / Engineering prototype**。
- 真實模型執行依賴本機憑證、網路、provider 與 budget authorization。
- SQLite 支援目前單機工程驗證，不代表多節點 production deployment。
- Tests 全綠是工程證據之一，不等於所有外部環境都已 production accepted。
- Founder-facing UI 應優先展示 outcome / state / team / blocker / next / action / cost；raw provider 與 internal IDs 留在 technical audit。

## Demo 前快速檢查

```powershell
python -m compileall -q eason_one scripts tests
python scripts/audit_v020_core.py
python scripts/audit_v020_governance.py
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD="1"
python -m pytest -q --basetemp=.pytest-eason-one-temp -p no:cacheprovider
```
