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
and the clock/allowance gates covered by the tests. At that revision, sleep/wake
behavior relied on persistent jobs being checked when the worker next ran.

## Installer and menu bar, version 0.2.0

- 31 tests pass, including normalized usage/reset data, unknown availability,
  full active queue presentation, live worker locks, real installed shell launchers,
  quoting paths with spaces/shell metacharacters, reinstall, unrelated-file protection,
  waiting for an old launchd menu bar agent to unload before replacing it,
  and creating the app Resources folder if archive extraction omitted the empty directory.
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
- Extracted the final zip into a new temporary folder and ran its `install.command`
  with custom paths and no login/shell changes. Both aliases worked, and the installed
  bundle passed signature verification without requiring a native rebuild.
- Native Computer Use inspection timed out. App process state and its actual
  refresh pipeline were verified directly; the SwiftUI panel is rendered by a
  separate native preview harness. Visually inspected both the live usage panel and
  a panel with a sample queued task, including full-prompt copy, workspace, and cancel controls.

## Menu bar composer and model/effort overrides, version 0.3.0

- 33 Python tests pass. Explicit model/effort survive CLI enqueueing and are passed
  to the Codex subprocess; unset effort leaves the existing Codex configuration alone.
- Live `model/list` discovery verified the account's model slugs and each model's
  supported reasoning efforts. `config/read` supplied configured defaults.
- Compiled and ran native composer integration checks against fake Codex. The exact
  bridge used by the UI saved a 190 KB multiline Unicode prompt without argument-size
  limits, model/effort, edit permissions, idle/window gates, and reset scheduling.
  Empty prompts were rejected; all temporary test jobs were cancelled.
- Visually inspected the native composer render, including prompt editor, workspace
  picker, model/effort controls, schedule modes, permission toggles, and queue action.

## Scheduled wake and run-time sleep hold

- Added a per-user macOS LaunchDaemon that schedules one-shot `pmset` wake events
  through an authenticated local socket. The worker reschedules a wake when a job
  waits for its allowed window, inactivity requirement, or usage allowance.
- Local Codex runs use `caffeinate -i` tied to the Codex process, allowing display
  sleep while holding off idle system sleep. Cloud dispatch releases the Mac after
  submission.
- The native app and helper compile successfully on 6 October 2026. The helper has
  been installed with administrator authorization. The worker LaunchAgent, menu bar
  LaunchAgent, and per-user system wake helper all report `running`; the installed app
  passes `codesign --verify --deep --strict`.
- No Schedulex job was queued during installation, so no timed wake event was added.
  Actual sleep/wake behavior, display state, and post-task sleep remain unverified.
