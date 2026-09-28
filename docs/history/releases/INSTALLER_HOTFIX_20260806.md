# Installer hotfix2 — 2026-08-06

The previous installer recursively backed up the entire repository with `robocopy`.
On this Windows repository, unreadable cache/test directories caused exit code 9.

Hotfix2 no longer recursively copies the whole repository. It:

- moves only application-owned code trees to a same-drive backup;
- preserves `.venv`, `.env`, `.git`, and the live `instance` directory in place;
- separately backs up `instance/eason_one.db` before migration;
- never replaces the live database with the package snapshot;
- restores the previous code and database automatically if installation fails.
