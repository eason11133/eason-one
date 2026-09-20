# Project Structure

這是一張閱讀地圖，不是完整檔案清單。EASON ONE 保留歷史 migration、release audit 與 acceptance evidence，因為它們是理解系統如何演進及如何被驗證的一部分。

## 根目錄

- `README.md`：教授與第一次造訪者的入口。
- `pyproject.toml`：Python 版本、runtime / test dependencies 與 package data。
- `run.py`：Flask application entry point；刻意停用 development reloader，避免產生兩個 runtime owner。
- `*.md` release / checkpoint 文件：歷次工程審查、產品 gate、失敗紀錄與 handoff evidence。它們不是 current source authority，但保留決策脈絡。

## `eason_one/`

- `__init__.py`：application factory、資料庫初始化、SQLite runtime 設定及相容性升級入口。
- `models.py`：Founder、Employee、Project、Work、Execution、Artifact、Verification、Decision、Meeting、Cost、Learning 等持久化模型。
- `routes.py`、`schemas.py`：HTTP boundary 與輸入／輸出 schema。
- `providers.py`、`provider_protocol.py`：模型 provider adapter 與回覆協定。
- `env_loader.py`：本機 `.env` 載入；真實 `.env` 被 Git 排除。
- `release_snapshot.py`：release/source snapshot 支援。
- `templates/`、`static/`：Founder-facing Headquarters 與操作介面。

## `eason_one/services/`

### 權威與生命週期

- `project_contract.py`：Founder Project Contract 與接受條件的權威來源。
- `company_kernel.py`、`company_runtime.py`：Company Core 的 deterministic progression 與 process-owned runtime shell。
- `projects.py`、`project_company.py`、`project_outcome.py`：Project 操作、公司投影與 evidence-based outcome。
- `work_execution.py`、`work_runtime.py`、`execution.py`：Work dispatch、execution attempt 與 runtime coordination。

### 治理、成本與可靠性

- `governance.py`、`proposal_authority.py`、`founder_decisions.py`：authority gate 與 Founder 決策。
- `work_budget.py`、`costs.py`、`model_configs.py`：預算 reservation、成本紀錄與 provider pricing configuration。
- `external_effects.py`、`runtime_recovery.py`、`host_validation.py`：副作用 receipt、restart recovery、reconciliation 與主機驗證。
- `acceptance_contract.py`、`reviews.py`、`artifacts.py`：接受條件、獨立 review、artifact version 與 verification evidence。

### Multi-Agent 公司能力

- `ceo_*.py`：CEO intent、planning、operating、review、learning 與 Founder 對話脈絡。
- `team_formation.py`、`workforce.py`、`employees.py`、`multi_agent.py`：Employee identity、組隊與協作。
- `meeting_kernel.py`、`meeting_coordination.py`、`meetings.py`：持久化會議、參與與協調。
- `research.py`、`research_department.py`：研究工作、provider routing 與 evidence capture。
- `employee_memory.py`、`learning.py`：由工作證據支撐的員工經驗與學習紀錄。
- `engineering_runtime.py`、`codex_connector.py`、`execution_policy.py`：工程工具連接、write scope 與 execution boundary。

### 相容層

- `legacy_company_runtime_v018.py`、`legacy_v015.py`、`core_v018.py`：歷史資料與窄範圍相容用途；不是 current v0.20 Project completion authority。

## `tests/`

- `test_v020_core_rebuild.py`、`test_v020_governance_*.py`、`test_v020_current_regressions.py`：目前 Company Core 與治理不變量。
- `test_v020_migration.py`：歷史資料升級與一次性 migration。
- `test_v020_multi_employee_company.py`：多人分工、artifact、verification 與 result evidence。
- `test_*recovery*.py`、`test_*performance*.py`：restart / retry / long-history read path。
- `acceptance/`：Founder-facing 與版本切片的 acceptance contract tests。
- 舊版 numbered tests：保留 regression coverage，避免新核心破壞既有能力。

## `scripts/`

- `audit_v020_core.py`、`audit_v020_governance.py`：核心與治理 source/test closure audit。
- `audit_*contracts.py`、`audit_*release*.py`：provider、review、first-trial 與 release contract 檢查。
- `migrate_*.py`：顯式資料 migration；正常 application boot 不應冒充完整歷史 migration。
- `reconcile_v020_*.py`：Founder authority、HTTP proof 與 Project evidence 的修復／對帳工具。
- `verify-*.ps1`、`verify_*.py`：Windows host 與版本驗證入口。
- `run-*.ps1`、`stop-headquarters.ps1`、`headquarters_launcher.py`：本機啟停。
- `install-*.ps1`、`rollback-*.ps1`：保留資料與 checkpoint 的版本安裝／回復流程。

## `docs/`

- `ARCHITECTURE.md`：目前角色與資料流。
- `ENGINEERING.md`：工程問題、取捨與證據索引。
- `acceptance/`：release contract 與 acceptance matrices。
- `artifacts/`：可公開、非 runtime database 的工程驗證產物。
- `V0.*.md`、`HEADQUARTERS_*.md`：版本演進紀錄；閱讀時需和 current source 對照。

## 不進 Git 的本機資料

`.env*`（除安全的 `.env.example`）、`.venv/`、`instance/`、SQLite / DB、logs、coverage、pytest temp、`.eason-one-backups/`、validation / update-state、simulation snapshots 與 `dist/` release bundles都由 `.gitignore` 排除。
