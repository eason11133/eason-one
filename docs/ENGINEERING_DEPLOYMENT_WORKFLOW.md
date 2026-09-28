# Engineering and deployment workflow

`D:\school\eason-one` is the confirmed deployment tree. Normal Codex product engineering must not modify its application source.

`D:\school\eason-one-dev` is the engineering Git worktree. Future candidate development and `FROM-S11` packaging happen there. Secrets, production databases, virtual environments, installer receipts, and deployment backups are not copied into the engineering tree.

Release sequence:

1. User confirms the currently installed deployment release.
2. Codex develops the next candidate only in `D:\school\eason-one-dev`.
3. The package embeds exact baseline images and guards for the confirmed deployment release.
4. The package installer runs only against `D:\school\eason-one`.
5. The baseline advances only after the user reports the installer success marker.

For S11, run `D:\school\eason-one\scripts\history\PREPARE_EXACT_S10_AND_INSTALL_S11.ps1`. It preserves the exact S11 engineering payload, restores only the eight S10 baseline files, invokes the guarded S11 installer, and creates the S12 engineering worktree only after successful installation.
