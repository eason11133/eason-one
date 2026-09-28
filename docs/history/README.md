# Engineering History

這個資料夾保存 EASON ONE 的歷史 checkpoint、hotfix、handoff、gate failure 與 review evidence。

這些內容是系統演進與除錯證據，不是 current source authority。第一次閱讀請先看：

1. `README.md`
2. `docs/ARCHITECTURE.md`
3. `docs/ENGINEERING.md`
4. current source / tests

## 結構

- `releases/`：從 repository root 收進來的歷史 release / hotfix / handoff / blocker / acceptance 文件。
- 這個目錄下其他既有 checkpoint 文件：保留原始演進脈絡。
- `scripts/history/`：不再屬於 current entry point、但仍值得保留的歷史安裝橋接腳本。

Public cleanup 只移動檔案位置，不刪除歷史證據；所有已知 script / test / doc references 都同步更新。
