# schedulex

A lightweight local CLI that queues complete prompts and runs Codex later using
your existing ChatGPT subscription login. It can dispatch either to a local
workspace or a configured Codex Cloud environment. Python 3.11+, no runtime
dependencies. macOS and Linux; optional macOS login service.

## macOS installer and menu bar

Double-click `install.command`, or run `./install.command` in Terminal. The installer
adds **both `schx` and `schedulex`** to `~/.local/bin`, installs `Schedulex.app` in
`~/Applications`, and starts the worker and menu bar app at login. Both commands
accept identical options. Open a new Terminal after installation. Python 3.11+
and a signed-in Codex CLI are required; the menu bar app needs macOS 14+.

The installer copies the runtime to `~/Library/Application Support/Schedulex`, so
the installed commands and login services work even if you move this repository.
It preserves the existing queue and results. The first background-service install
asks for administrator authorization to add timed-wake support. The source installer
builds the native app using Apple's command line tools if a matching prebuilt app
isn't present. The installer zip includes a prebuilt app for the architecture named
in its filename.

The clock/checkmark menu bar icon shows scheduled tasks, worker state, remaining
subscription allowance, absolute reset times and countdowns, and the authoritative
number of banked usage resets, including expiry dates when reported by Codex.
It refreshes every minute and when opened. Failed reads show an error and label
previous usage data as stale; unknown counts are shown as unavailable, not zero.
Banked resets are never automatically consumed. Click **Queue a task** to enter a
complete prompt, choose a Codex project folder, schedule a date/time, the next five-hour reset, or the weekly reset,
and optionally restrict starts to overnight hours or 15 minutes of inactivity.
Project edits require the **Allow edits in this project** toggle. A queued task
appears in the task list; scheduling does not start a stopped worker automatically.
The menu bar app asks macOS for notification permission and schedules alerts ten
minutes before reported usage-window resets and pending tasks. If it first sees a
reset or task less than ten minutes away, it alerts as soon as possible. Alerts
are refreshed while the menu bar app is running; manage permission in System Settings.
You can edit or reschedule pending jobs, cancel them, copy
their complete prompt, open workspaces/results, or start/stop the worker there.
Quitting the menu bar app leaves the worker running.

```sh
schx add --at +2h --prompt-file prompt.txt --sandbox workspace-write
schedulex list
schx menubar
schx models
schx add --at +2h --model gpt-6.1-sol --effort high --prompt-file prompt.txt
schx add --at +2h --cloud-env ENV_ID --prompt-file prompt.txt
```

Use `--cloud-env ENV_ID` to queue a task for Codex Cloud. Find available
environment IDs with `codex cloud`; each environment supplies its own GitHub
repositories, setup, permissions, and network access. Schedulex keeps the
scheduled prompt in its local queue, then submits it with `codex cloud exec`
when due. The local worker and computer must be available at dispatch time.
After Codex accepts the task, it runs remotely while this computer is asleep.
The Schedulex task then shows **Submitted** and links to the Cloud task when the
CLI returns its URL. Schedulex cancellation works only before dispatch; track
or continue a submitted task in Codex Cloud.

Both the CLI and menu bar support per-task model and reasoning effort overrides.
Use `--model` and `--effort` (alias `--reasoning-effort`) in the CLI. In the composer,
choose a model from the live Codex catalog or enter its slug, then choose an effort.
Available effort levels come from the selected model's reported capabilities. For
a custom model or unavailable catalog, you can enter an effort manually. Changing
models clears the previous effort selection. Leave model/effort blank or select
**Codex default** to retain Codex configuration. Overrides are saved with each job
and displayed on task cards. Unsupported custom combinations are handled by Codex
at execution time. `schx models` lists live model capabilities and configured defaults.
Drafts are retained while navigating back; successful queueing clears the prompt.

To install without starting anything or changing your shell profile:

```sh
./install.command --no-start --no-shell
```

Custom paths are supported with `--prefix`, `--bin-dir`, `--app-dir`, and
`--state-dir`. The menu bar observes the queue chosen at installation; CLI queue
overrides don't change the app's configured queue. `schx dashboard` exposes the
same normalized JSON used by the app. The app's last refresh is saved locally in
`menubar-status.json` beside the queue for diagnostics.

To stop automatic startup, run `schx service uninstall` for the worker, and remove
the menu bar login agent with `launchctl bootout gui/$(id -u)/local.schedulex.menubar`,
then remove `~/Library/LaunchAgents/local.schedulex.menubar.plist`. The queue stays
on disk. Re-running the installer updates the managed app/runtime and restarts
the services; review running jobs first because updating interrupts the worker.

Build a distributable archive with `python3 scripts/make_installer.py`. The local
app is ad-hoc signed; Developer ID signing/notarization is not configured.

## Start

Install the [Codex CLI](https://learn.chatgpt.com/docs/codex-cli), then `codex login`
with ChatGPT. Sign in with a subscription, rather than an API key.

```sh
python3 -m pip install -e .
schedulex limits
schedulex add --at +2h --project /path/to/project --prompt-file prompt.txt --sandbox workspace-write
schedulex worker
```

Without installing, replace `schedulex` with `python3 -m schedulex` from this folder.
If Codex is not on PATH, use `--codex /absolute/path/to/codex` before the command.

Write the prompt fully as you normally would. It is saved unchanged, including
newlines and Unicode. Pass a quoted prompt, a UTF-8 file, or pipe stdin:

```sh
schedulex add --at '2026-10-07T02:00:00+01:00' --project /path/to/project <<'PROMPT'
Review this project and fix the failing tests.
Explain what changed and verify the result.
PROMPT
```

The default sandbox is read-only. Add `--sandbox workspace-write` to authorize
project edits. `--project PATH` (also available as `--cwd PATH`) selects the local
Codex project folder and is saved with the scheduled task. That folder is the task's
project context and working directory. Local
tasks run on Codex’s shared app-server daemon, load that project's instructions, and
appear as persistent chats in the Codex app. Use **Open in Codex** on a task card to
review saved history or continue the chat after completion. Use **Interrupt** in
Schedulex or `schx interrupt JOB_ID` to stop a running turn. Some desktop versions
use a separate server and cannot control daemon-owned runs with their stop button.
This uses
the experimental Codex App Server interface, so a current Codex CLI with
`app-server daemon start` and `app-server proxy` support is required. Schedulex
reports connection failures instead of falling back to an invisible run.

Every run is noninteractive with approval policy `never`; commands
outside its sandbox cannot wait for an overnight approval. No unrestricted mode
is exposed. Codex still loads its normal project instructions and user settings;
Schedulex overrides the model provider to OpenAI. Model selection defaults to your
Codex configuration, or use `--model`. Select the corresponding metered bucket
with `--limit-id` if your chosen model uses a different bucket (`limits` lists them).

## Run after the five-hour reset

```sh
schedulex add --after-reset --project /path/to/project \
  --window 23:00-07:00 --timezone Europe/London --idle-minutes 15 \
  --sandbox workspace-write --prompt-file prompt.txt
```

This captures the next actual five-hour reset timestamp reported by Codex, adds
a 30-second buffer, and starts no earlier than that. The worker rechecks live
allowance just before execution. Unknown limits, missing login, network errors,
exhausted weekly limits, or insufficient allowance leave the job pending with a
visible reason. It never guesses a reset by adding five hours to the current time.

To schedule for the weekly limit reset instead, choose **After weekly reset** in
the menu bar app or use:

```sh
schedulex add --after-weekly-reset --project /path/to/project --prompt-file prompt.txt
```

This uses the reported weekly reset for the selected usage bucket, with the same
30-second buffer and allowance checks. If that reset time is unavailable, queuing
fails with a clear error; use `--at` to choose a time yourself. Both reset options schedule a single task. Use the reset recurrence options below for repeated runs.
If no five-hour timestamp is reported, use `--at` instead.

`--window` restricts job starts to those hours in the named IANA timezone, including
overnight ranges and daylight saving changes. Outside the window it waits for the
next window. `--idle-minutes` checks macOS keyboard/mouse inactivity before starting.
It does not detect whether other Codex sessions are generating, or stop a task
when you return. Jobs may continue past the end of the window. Omit either option
when that restriction is unnecessary. `--min-remaining 20` requires at least 20%
remaining in every reported window before a job starts.

This schedules ordinary subscription usage, not additional quota. Usage is shared
with your other Codex sessions; weekly limits still apply. A preflight cannot reserve
allowance or guarantee a task finishes before quota runs out. Schedulex does not
redeem reset credits or purchase credits. An account with automatic paid-credit
fallback configured should disable that setting if it must avoid paid usage.
It does not call the separately billed OpenAI API with an API key.

## Edit, reschedule, and repeat after usage resets

Click **Edit** on a pending task to change its prompt, destination, project, model,
permissions, schedule, or recurrence, then **Save changes**. The job keeps its ID.
Changes are rejected if the task starts running or another edit changes it while
its form is open; reopen the task to load the latest version. Unchanged settings
are retained, including custom CLI hours, inactivity thresholds, and usage gates.

```sh
schx edit JOB_ID --at +3h
schx edit JOB_ID --prompt-file updated-prompt.txt
schx edit JOB_ID --model gpt-6.1-sol --effort high
schx edit JOB_ID --clear-model --clear-effort
```

In the composer, choose **Every 5-hour reset** or **Every weekly reset** under
**Repeat**. These are the only recurring schedules; there are no fixed-hour,
daily, or calendar-week recurrence options.

```sh
schx add --repeat-reset five-hour --project /path/to/project --prompt-file prompt.txt
schx add --repeat-reset weekly --project /path/to/project --prompt-file prompt.txt
schx edit JOB_ID --repeat-reset weekly
schx edit JOB_ID --at +2h  # switch to a one-shot task
```

Each occurrence starts no earlier than the matching reset reported by Codex plus
30 seconds, subject to allowance, allowed hours, and inactivity checks. After a
successful local run, a new pending occurrence is saved with its own ID and history.
The worker waits for a distinct future reset reported for the same usage bucket;
it never estimates a reset by adding five hours or seven days. If the timestamp
is missing or stale, the pending task shows the reason and waits. Missed resets
do not create catch-up runs. Editing the prompt of an existing recurring task
preserves its captured reset. Cancelling its pending occurrence stops the series.
Failures, timeouts, and interruptions stop recurrence for review rather than
retrying a task that may have already changed files.

For Cloud tasks, successful submission schedules the next occurrence. Schedulex
cannot track remote completion or prevent overlapping Cloud executions.

The menu bar app also sends notifications when a local task completes, fails, or
is interrupted, and when a Cloud task is submitted. Click a notification to open
the saved chat, Cloud task, or results. Delivery is checked each minute, including
while the composer is open, and remembers notified runs across app restarts.
The first launch establishes a baseline without replaying old history. Later
launches notify about runs finished while the app was closed. macOS notification
permission is required; Cloud submission is labelled as submission, not completion.

## Background worker

For macOS, install the background worker:

```sh
schedulex service install
schedulex service status
schedulex service uninstall
```

The first install asks for administrator authorization to add Schedulex's small
scheduled-wake helper. It uses macOS power events to wake the computer for a queued
job. The worker schedules pending tasks, removes a wake when a task is canceled, and
reschedules one when a task must wait for its time window, inactivity, or Codex
allowance. `service uninstall` removes pending Schedulex wake events and the helper;
your queue and results stay saved.

For a local Codex run, Schedulex prevents idle system sleep only while Codex is
running. The display may still turn off. When Codex finishes, Schedulex releases the
sleep hold and normal macOS sleep settings apply. A Codex Cloud task only needs the
Mac awake long enough to submit the task; Cloud continues remotely afterward.

The Mac must be sleeping rather than shut down, online, and logged into the user
account that runs Schedulex. Keep the configured Python, source, Codex, and queue
paths available; reinstall after moving them. macOS or the hardware may defer or
ignore a timed wake in some power states, and the display may turn on depending on
the Mac and its settings. A LaunchAgent also needs access to the project directory;
macOS privacy permissions may require local setup.

On Linux run `schedulex worker` in a persistent user session. Foreground mode works
on macOS too, but timed wake support requires the installed macOS background service.
Closing a foreground worker stops it; the queue stays saved.

## Manage and review

```sh
schedulex list
schedulex show JOB_ID
schedulex cancel JOB_ID
schedulex worker --once
```

Jobs run one at a time. A lock prevents two workers on the same queue. State is in
`~/.local/share/schedulex` by default. Set `SCHEDULEX_HOME` or pass `--state-dir` before
the command to choose another location. Each run stores `answer.txt`, `events.jsonl`,
and `stderr.log` under `runs/JOB_ID`; `show` prints their directory and the full prompt.
Prompts and logs are private local plaintext files. Schedulex never copies login
tokens; it delegates authentication to Codex.

Tasks are one-shot by default. Failed, timed-out, or interrupted runs are never automatically
retried because they may already have changed files. Review the workspace and logs,
then enqueue a new task if needed. Pending jobs can be cancelled. To interrupt an
active job, stop the foreground worker with Ctrl-C or uninstall its background
service, click **Interrupt** in Schedulex, or run `schx interrupt JOB_ID`. SIGTERM/Ctrl-C request
`turn/interrupt` on the shared server before closing the worker connection. An abrupt kill or power
loss marks a running job interrupted at the next worker startup; child processes
can survive an abrupt worker kill, so inspect processes before scheduling again.

`--timeout` defaults to two hours. The worker polls every minute (`--interval`).
Only start times are scheduled, not a guarantee of exact execution time.

## Verification

```sh
python3 -m unittest discover -s tests -v
# Optional macOS integration check; starts and removes a temporary fake-Codex service:
python3 -m tests.verify_launchd
```

The tests use a fake Codex executable to verify the actual app-server handshake,
subscription checks, prompt transport, schedule gates, successful/failed execution,
and persistence without spending allowance. Run `schedulex limits` for a read-only
check against your real login. To verify inference locally, queue a read-only
prompt for `+0m` and run `worker --once`; that consumes subscription usage.

Protocol references: [Codex App Server](https://learn.chatgpt.com/docs/app-server)
and [non-interactive execution](https://learn.chatgpt.com/docs/non-interactive-mode).
