# Gotcha registry

Non-obvious behaviors and fixes discovered while developing Terminal Agent Launcher.

## Categories

- Browser protocol handling
- Folder discovery
- iTerm2 integration
- macOS LaunchAgent

## Registry

### UI-001 Author CSS can override the hidden attribute

**Symptom:** Loading skeletons remain visible after project rows load.

**Cause:** A component rule such as `.loading-state { display: grid; }` overrides the browser's built-in `[hidden]` styling.

**Fix:** Keep the global `[hidden] { display: none !important; }` rule.

**Discovered:** 2026-08-28

### TESTING-001 Default unittest discovery needs an importable tests directory

**Symptom:** `python3 -m unittest discover -v` exits successfully after running zero tests.

**Cause:** The local Python discovery path does not import `tests/test_server.py` unless `tests` is a package.

**Fix:** Keep `tests/__init__.py` and confirm the reported test count is nonzero.

**Discovered:** 2026-08-28

### ITERM-001 Create a default session before writing the launch command

**Symptom:** Starting a command directly through an iTerm2 profile override can bypass the user's normal login-shell setup or close the tab when the command exits.

**Cause:** A profile `command` replaces the usual shell process.

**Fix:** Create the default-profile tab or window first, then use `write text` in its current session. Pass the command as an `osascript` argument instead of interpolating it into AppleScript source.

**Discovered:** 2026-08-28

### PACKAGING-001 Do not combine a PEP 639 license expression with a license classifier

**Symptom:** Building the wheel fails with `InvalidConfigError` after adding `license = "MIT"`.

**Cause:** Setuptools 77 and newer reject the older `License :: OSI Approved :: MIT License` classifier when a PEP 639 SPDX license expression is present.

**Fix:** Keep `license = "MIT"` and remove the redundant license classifier. Verify installation from outside the source tree so local imports cannot hide missing package data.

**Discovered:** 2026-08-29

### CI-001 GitHub Actions can pass while running deprecated action runtimes

**Symptom:** CI succeeds but annotates every JavaScript action because its major version targets a deprecated Node runtime.

**Cause:** Older `actions/checkout`, `actions/setup-python`, and `actions/setup-node` releases bundle their own Node runtime independently of the project Node version configured in the workflow.

**Fix:** Pin current action releases by full commit SHA, keep the version in an inline comment, and enable Dependabot updates for the `github-actions` ecosystem.

**Discovered:** 2026-08-29

<!--
### CATEGORY-001 Title

Symptom: What you observe.

Cause: Why it happens.

Fix: What to do.

Discovered: YYYY-MM-DD
-->
