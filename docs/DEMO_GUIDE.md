# Eason One 第一階段 Demo Guide

這份流程用 current repository 的真實頁面與 durable data 展示 Eason One；不需要觸發付費 provider，也不會把 engineering prototype 描述成 production-ready 產品。

## 啟動

Windows PowerShell：

```powershell
.\.python313\python.exe run.py
```

若是全新資料庫，先執行：

```powershell
.\.python313\python.exe -m flask --app run.py seed
```

開啟 `http://127.0.0.1:5000/headquarters`。正常冷啟動應在數秒內完成；頁面 GET 不應觸發 provider call、runtime reconciliation 或 durable mutation。

## 5 分鐘 Demo

1. **HQ 看公司現在的狀態**：說明 Founder、CEO、Project、Work、Verification、Result、Learning 的責任鏈；第一屏只呈現 current company truth。
2. **Founder 給一個真實目標**：說明 request 先成為受治理的 Project intent，不要在沒有 provider 憑證或預算授權時現場觸發付費執行。
3. **看 CEO 建立與推進 Project**：進入 Projects，展示目標、Contract、目前狀態與下一步。打開 Project #22（若存在），確認是 `WAITING` 而非 `WORKING`。
4. **看 Work 分給不同 Employee / Agent**：在 Project flow 指出 delivery、research、review 與 management Work 的責任不同；management `EXECUTING` 是 ownership envelope，不等於 provider 正在執行。
5. **看 execution / verification / recovery**：展開 Project details & audit，指出 Run、ArtifactVersion、VerificationRecord 與 WaitCondition 的證據鏈。
6. **看 Founder gate**：只有 budget、scope 或人類判斷等 canonical authority 才進 Needs You。Missing evidence 若沒有新 truth，應 quiesce，而不是製造假的 Founder gate 或無限重試。
7. **看 Result / cost / audit trail**：到 Results、Finance、Research/System；結果保留驗證依據，Finance 是 local model cost 與 authority ledger，不是假稱 provider 帳單。

## 教授如果只看三個地方

1. [`README.md`](../README.md) 與 [`ARCHITECTURE.md`](ARCHITECTURE.md)：產品問題、角色與完整資料流。
2. `eason_one/services/company_kernel.py`、`governance.py`、`project_contract.py`、`work_execution.py`：Work 推進、Founder-only authority、Contract 與執行邊界。
3. `tests/test_ceo_operating_system.py`、`tests/test_v020_governance_floor.py`、`tests/test_v020_current_regressions.py`：missing-evidence quiescence、治理 fail-closed、recovery / replay regression。

Project #22 的 Founder-facing 說明應為「目前缺少可驗證的既有證據，因此這個研究分支已停止」，並保留 `BLOCKED_MISSING_EVIDENCE` 技術碼。重新整理頁面不應新增 AgentRun、成本或外部副作用。

## Demo 中應明確說明的邊界

- Eason One 目前是 **Active development / Engineering prototype**。
- 真實模型執行依賴本機憑證、網路、provider 與預算授權。
- SQLite 支援目前單機驗證，不代表多節點 production deployment。
- `WAITING` 可以是正確的 quiescent 狀態；缺證據時停止，比製造 busywork 或無界重試更可信。
- 測試全綠是工程證據之一，不等同於所有外部環境的 production acceptance。

## Demo 前快速檢查

```powershell
.\.python313\python.exe -m compileall -q eason_one scripts tests
.\.python313\python.exe scripts\audit_v020_core.py
.\.python313\python.exe scripts\audit_v020_governance.py
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD="1"
.\.python313\python.exe -m pytest -q --basetemp=.pytest-eason-one-temp -p no:cacheprovider
```

若 Project #22 不存在，使用任一具有 missing-evidence Wait Condition 的 current Project 展示相同語意；不要為了 demo 修改 durable truth。
