# Engineering Notes

EASON ONE 的難點不在「呼叫多個模型」，而在模型呼叫前後的權威、狀態、證據與失敗語意。以下只整理 current source 與 tests 中能找到證據的設計。

## 1. 把模型建議與系統權威分開

模型適合處理語意理解、規劃草案與內容生成，但不適合單獨決定 durable state transition。EASON ONE 讓 Founder Project Contract 保存目標、限制與接受條件；CEO 的 proposal 必須在 authority gate 內轉成 Work。完成判定由 `project_outcome.py` 依 accepted artifact 與 verification evidence 推導，而不是相信模型輸出的「done」。

證據入口：`project_contract.py`、`proposal_authority.py`、`founder_decisions.py`、`project_outcome.py`、`tests/test_v020_core_rebuild.py`。

## 2. Multi-Agent 分工需要工作圖，不只是多個 prompt

如果只有多次 API call，責任、相依關係與交付物仍不清楚。系統以 Project → Work graph 表達 management、delivery、research、review 等工作，記錄 assigned Employee、manager 與 execution snapshots；平行分支也要在共同預算與 completion rule 下運作。

取捨是增加狀態機與資料模型複雜度，換取可追蹤分工、獨立驗證與失敗隔離。證據入口：`multi_agent.py`、`team_formation.py`、`work_execution.py`、`tests/test_v020_multi_employee_company.py`。

## 3. 付費副作用不能用「重試看看」處理

Provider request 可能已送達但 process 在保存回覆前中斷。直接 retry 可能重複扣款或重複產生外部效果。因此 execution 會保存 attempt、provider response ID、成本快照與 external-effect receipt；恢復時先判斷是否需要 reconciliation。預算則在呼叫前 reserve，避免平行 Worker 各自看到同一份餘額。

這套設計不能消除 provider 的所有不確定性，但能把 ambiguous state 顯式化。證據入口：`external_effects.py`、`work_budget.py`、`runtime_recovery.py`、`tests/test_v020_current_regressions.py` 與 paid/provider contract audits。

## 4. Idempotency 是資料約束與語意約束

只在 application code 判斷「好像做過」不足以抵抗 restart 或 race。專案同時使用 logical keys、unique index、version / lease、receipt 與 event lineage。對不能安全重播的動作採 bounded recovery；對已存在的成果，投影可以補建，但不重新觸發 provider。

取捨是需要保存更多 lineage，並明確區分 intent、dispatch、provider completion、artifact materialization 與 verification。證據入口：`models.py`、`company_events.py`、`company_kernel.py`、`runtime_recovery.py`。

## 5. Verification 必須綁定特定 Artifact Version

「review 通過」若沒有對象、版本與證據，之後內容改動仍可能沿用舊結論。EASON ONE 將 Artifact 與 ArtifactVersion 分開，Verification Record 指向具體版本；Project Outcome 也檢查 proof hash、producing work 與 contract criteria。Producer 與 reviewer 的責任可以分離。

證據入口：`artifacts.py`、`acceptance_contract.py`、`reviews.py`、`project_outcome.py`、review evidence contract audit。

## 6. Founder boundary 是 deliberate constraint

完全自主不是此專案的預設成功條件。當工作超出已授權 scope、接受條件缺失、預算不足、需要雇用／變更組織或存在價值判斷時，系統應產生明確 gate，而不是讓 Agent 猜測。寫入端會重新驗證 gate identity 與 current state，以防 stale approval。

取捨是某些流程會停下等待人類，但這讓責任與成本更清楚。證據入口：`governance.py`、`escalations.py`、`founder_decisions.py`、`tests/test_v020_governance_floor.py`、`tests/test_v020_governance_sweep.py`。

## 7. Process restart 後仍要能解釋現在在哪裡

Runtime thread 只負責推進，不是權威來源。Project、Work、Execution、Meeting、Wait Condition 與 checkpoint 存在資料庫；lease 用於區分目前 owner，restart 會掃描 durable state，再進行 adopt / reconcile。SQLite 設定 WAL、busy timeout 與 foreign keys，以支援目前單機多工作分支，但文件不把它描述成分散式 production architecture。

證據入口：`company_runtime.py`、`work_runtime.py`、`runtime_recovery.py`、`eason_one/__init__.py` 與 recovery tests。

## 8. 歷史演進與 migration 不能混進正常啟動

Repository 包含多個世代的 schema 與 runtime。Current core 保留窄範圍 compatibility layer，但一次性的歷史轉換由 `scripts/migrate_v020.py` 等顯式執行；正常啟動不應反覆 replay migration 或讓 legacy completion semantics 重新取得權威。

這也是為何 repository 保留 migration tests、release constitution 與 rollback scripts。證據入口：`eason_one/__init__.py`、`legacy_company_runtime_v018.py`、`scripts/migrate_v020.py`、`tests/test_v020_migration.py`。

## 9. 長歷史與讀取投影

當事件、run、artifact 與 message 累積後，Founder page 不能每次載入都重做無界限掃描或意外改寫狀態。專案包含 read-path performance、projection coherence 與 nonblocking readiness tests，並將讀取表面與 mutation path 分離。

證據入口：`company_truth.py`、`current_company.py`、`context.py`、`tests/test_v0152_read_path_performance.py`、`tests/test_cross_surface_coherence.py`。

## 驗證策略與其邊界

驗證分成幾層：Python compile、source closure audits、focused invariant tests、完整 pytest，以及需要真實 provider / host / Founder 操作的 acceptance。單元測試全綠不等於真實 Project E2E 已驗收；相反地，外部 provider 暫時不可用也不應促使 production semantics 被改成假成功。

目前仍有環境邊界：真實付費 provider、網路研究、主機工程工具與某些 recovery path 只能在具相應設定的環境驗證。因此本專案對外定位是 Active development / Engineering prototype。
