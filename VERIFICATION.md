# Verification

Verified on macOS on 6 October 2026 with Python 3.14.7 and Codex CLI 0.160.0.

- 21 automated tests passed using an actual fake-Codex subprocess: app-server
  initialization, subscription enforcement, reset lookup, allowance checks,
  full Unicode/multiline prompt transport, scheduling windows, idle checks,
  cancellation, persistence, serial execution, worker locking, failures, timeouts,
  crash recovery, SIGTERM handling, and service configuration.
- Built and installed the Python wheel into a temporary directory; its `schedulex`
  console entry point launched successfully from outside the source directory.
- Read the real ChatGPT subscription account and live five-hour/weekly limits
  through `account/read` and `account/rateLimits/read`.
- Executed a queued read-only prompt through the real `codex exec`. The job reached
  `succeeded`, exit code 0, and its saved answer was `SCHEDULEX_OK`.
- Enqueued a real `--after-reset` job with overnight/idle restrictions. It captured
  the reported five-hour reset plus the 30-second buffer. Cancelled it after inspection;
  it did not run inference.
- Bootstrapped a temporary macOS LaunchAgent. It launched the worker and executed
  a future queued prompt using fake Codex. Verified exact prompt transport, then
  unloaded and removed the temporary service.

The original CLI verification did not install the persistent service. A multi-hour wait until a real
quota reset was not performed; reset scheduling uses the live reported timestamp
and the clock/allowance gates covered by the tests. Sleep/wake behavior relies on
persistent jobs being checked when the worker next runs; no automatic wake is offered.

## Installer and menu bar, version 0.2.0

- 30 tests pass, including normalized usage/reset data, unknown availability,
  full active queue presentation, live worker locks, real installed shell launchers,
  quoting paths with spaces/shell metacharacters, reinstall, unrelated-file protection,
  and waiting for an old launchd menu bar agent to unload before replacing it.
- Native SwiftUI app builds for macOS 14+ using the installed stable SDK. Its
  bundle passes `codesign --verify --deep --strict` after installation.
- Installed both `schx` and `schedulex` in `~/.local/bin` and verified their commands
  from `/tmp`. Installed app in `~/Applications/Schedulex.app`; its configuration
  points to the copied runtime, not the repository.
- Both persistent LaunchAgents are running. Worker launch configuration uses the
  runtime in `~/Library/Application Support/Schedulex`.
- The running menu bar app wrote its own successful live dashboard refresh:
  Plus plan, five-hour and weekly allowance, exact reset timestamps, and one banked
  reset with expiry. No banked reset was redeemed.
- Generated an architecture-specific installer zip with the prebuilt native app.
- Native Computer Use inspection timed out. App process state and its actual
  refresh pipeline were verified directly; the SwiftUI panel is rendered by a
  separate native preview harness. Visually inspected both the live usage panel and
  a panel with a sample queued task, including full-prompt copy, workspace, and cancel controls.
