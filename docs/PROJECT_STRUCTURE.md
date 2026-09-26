# Project Structure

這是一張閱讀地圖，不是完整檔案清單。Current source / tests 是系統權威；歷史 checkpoint 與 review 被保留作為演進證據，但不應和 current truth 混在一起閱讀。

## 根目錄

- `README.md`：第一次造訪者與教授的入口。
- `pyproject.toml`：Python 版本、runtime / test dependencies 與 package metadata。
- `run.py`：Flask application entry point。
- `.github/workflows/tests.yml`：main / pull request 的 GitHub Actions 測試。
- 少數 release-owned root 文件仍保留在根目錄，因為既有 installer / rollback scripts 會直接引用它們。
- 大量歷史 checkpoint、gate failure 與外部 review 已集中到 `docs/history/`，避免把 current landing page 淹沒。

## `eason_one/`

- `__init__.py`：application factory、資料庫初始化與 SQLite runtime 設定。
- `models.py`：Founder、Employee、Project、Work、Execution、Artifact、Verification、Decision、Meeting、Cost、Learning 等持久化模型。
- `routes.py`、`schemas.py`：HTTP boundary 與輸入／輸出 schema。
- `providers.py`、`provider_protocol.py`：模型 provider adapter 與回覆協定。
- `env_loader.py`：本機 `.env` 載入；真實 `.env` 被 Git 排除。
- `templates/`、`static/`：Founder-facing Headquarters / Project / Results / System 等 UI。

## `eason_one/services/`

### 權威與生命週期

- `project_contract.py`：Founder Project Contract 與接受條件。
- `company_kernel.py`、`company_runtime.py`：deterministic progression 與 runtime shell。
- `projects.py`、`project_company.py`、`project_outcome.py`：Project 操作、Founder projection 與 evidence-based outcome。
- `work_execution.py`、`work_runtime.py`、`execution.py`：Work dispatch、Execution attempt 與 runtime coordination。

### 治理、成本與可靠性

- `governance.py`、`proposal_authority.py`、`founder_decisions.py`：authority gate 與 Founder 決策。
- `work_budget.py`、`costs.py`、`model_configs.py`：預算 reservation、成本紀錄與 pricing configuration。
- `external_effects.py`、`runtime_recovery.py`、`host_validation.py`：receipt、restart recovery、reconciliation 與主機驗證。
- `acceptance_contract.py`、`reviews.py`、`artifacts.py`：接受條件、review、Artifact Version 與 Verification evidence。

### Multi-Agent 公司能力

- `ceo_*.py`：CEO intent、planning、operating、review、learning 與 Founder 對話。
- `team_formation.py`、`workforce.py`、`employees.py`、`multi_agent.py`：Employee identity、capacity、組隊與協作。
- `meeting_*.py`：持久化會議與協調。
- `research.py`、`research_department.py`：研究工作、provider routing 與 evidence capture。
- `employee_memory.py`、`learning.py`：由工作證據支撐的員工經驗。
- `engineering_runtime.py`、`codex_connector.py`、`execution_policy.py`：工程工具、write scope 與 execution boundary。

### Compatibility

`legacy_company_runtime_v018.py`、`legacy_v015.py`、`core_v018.py` 等只服務歷史資料與窄範圍 compatibility，不是 current v0.20 Project completion authority。

## `tests/`

- `test_ceo_operating_system.py`：CEO semantic progress、quiescence、capacity 與 Founder-facing behavior。
- `test_v020_core_rebuild.py`：Company Core v0.20。
- `test_v020_governance_*.py`：governance invariants。
- `test_v020_current_regressions.py`：recovery / replay / provider 等 current regressions。
- `test_v020_multi_employee_company.py`：多人分工、Artifact、Verification 與 Result evidence。
- `acceptance/`：Founder-facing / release acceptance contracts。
- 舊版 numbered tests 保留 regression coverage。

## `scripts/`

- `audit_v020_core.py`、`audit_v020_governance.py`：core / governance audit。
- `audit_*contracts.py`、`audit_*release*.py`：provider、review、release contract 檢查。
- `migrate_*.py`：顯式資料 migration。
- `reconcile_v020_*.py`：authority / HTTP proof / Project evidence 對帳。
- `run-headquarters.ps1`、`stop-headquarters.ps1`：Windows 本機啟停。
- `install-*.ps1`、`rollback-*.ps1`：歷史版本安裝與回復工具。

## `docs/`

- `ARCHITECTURE.md`：current roles、authority 與資料流。
- `ENGINEERING.md`：工程問題、取捨與證據索引。
- `DEMO_GUIDE.md`：Founder-facing demo。
- `history/`：歷史 checkpoint、gate failure、handoff 與 review。
- 其他 `V0.*.md`、`HEADQUARTERS_*.md`：版本演進紀錄，閱讀時需和 current source 對照。

## 不進 Git 的本機資料

`.env*`（除安全的 `.env.example`）、virtualenv、`instance/`、SQLite / DB、logs、coverage、pytest temp、backup / validation / update-state、release bundle 與 OS/editor junk 都由 `.gitignore` 排除。
