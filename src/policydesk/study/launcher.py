"""Starting the two web apps alongside the terminal tutor.

Why this exists
---------------
The desktop icon used to open the tutor and nothing else, so the two Streamlit
apps had to be started by hand in two more terminals. That is three things to
remember before you can study, which is three chances to not bother.

So the icon now starts everything: the study interface, the app itself, and the
tutor in the window you clicked.

Two servers, one tab
--------------------
Both servers start, but only ONE browser tab is opened. Opening two produced the
bug this module was then rewritten for -- see `start_study_apps`.

The four rules this module follows
---------------------------------
1. **Reuse a port that is already serving.** Double-clicking the icon twice, or
   having run `policydesk study` earlier, must not fail and must not start a
   second copy. If something is already answering on the port, we use it.

2. **Only stop what we started.** If you had a server running already, it is
   still running when the tutor closes. Terminating a process this program did
   not create would be a surprising thing for a learning tool to do.

3. **A web app that will not start must not block the tutor.** The tutor is the
   point; the browser tab is a convenience. A failure here is printed, with the
   log path, and then we carry on -- never swallowed, never fatal.

4. **Never depend on the browser having cooperated.** Handing a URL to the shell is
   a request, not a guarantee, so every address is printed as well.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


def _timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


PROJECT_ROOT = Path(__file__).resolve().parents[3]
LOG_DIR = PROJECT_ROOT / "logs"

# How the study app learns where the app itself is listening. Only the launcher
# knows the port, and only at launch, so it is passed down rather than guessed.
# Absent (running `policydesk study` on its own) the link is simply not shown --
# a link to a port with nothing behind it is worse than no link.
APP_URL_ENV = "POLICYDESK_APP_URL"

# How long to wait for Streamlit to answer before giving up on it. Streamlit's
# first start is the slow one: it imports torch-sized dependency trees and builds
# its static assets cache.
STARTUP_TIMEOUT_S = 60.0

# Gap between opening one tab and the next.
#
# Not cosmetic. On Windows `webbrowser` is `WindowsDefault`, which is `os.startfile`
# -- it hands the URL to the shell and returns immediately, having no idea whether a
# browser exists yet. Two of those back to back, with the browser still cold
# starting, and the second URL lands on a window that is not ready for it: the
# reported symptom was two tabs showing the SAME app, and no study tool anywhere.
#
# So the tabs are opened one at a time with a real gap, and the URLs are printed as
# well, because a browser handoff is a request and never a guarantee.
BROWSER_OPEN_GAP_S = 3.0


def port_is_serving(port: int, host: str = "127.0.0.1") -> bool:
    """True when something accepts a TCP connection on this port.

    A connection attempt rather than a parse of `netstat`: it asks the question we
    actually care about -- can a browser reach this -- and it is the same check on
    every platform.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.4)
        return probe.connect_ex((host, port)) == 0


def wait_until_serving(port: int, timeout: float = STARTUP_TIMEOUT_S) -> bool:
    """Poll until the port answers, or the timeout runs out.

    Waiting matters: a browser tab opened before Streamlit is listening shows a
    connection error, and the natural reading of that is "the thing is broken"
    rather than "the thing is still starting".
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_is_serving(port):
            return True
        time.sleep(0.4)
    return False


@dataclass
class AppProcess:
    """One web app we are responsible for, or one we are merely borrowing."""

    name: str
    port: int
    url: str
    process: subprocess.Popen[bytes] | None = None
    log_path: Path | None = None
    reused: bool = False

    @property
    def ours(self) -> bool:
        """True only when this program started it, so only it may stop it."""
        return self.process is not None


def start_app(
    name: str, script: Path, port: int, extra_env: dict[str, str] | None = None
) -> AppProcess:
    """Start one Streamlit app, or adopt the one already on that port."""
    url = f"http://localhost:{port}"

    if port_is_serving(port):
        return AppProcess(name=name, port=port, url=url, reused=True)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{name}.log"

    # Streamlit's own output goes to a file rather than into the tutor's window,
    # where it would scroll the lesson away. The path is printed, so a failure is
    # still followable -- the output is redirected, not discarded.
    #
    # APPENDED, with a header per launch. It was opened "wb" first, which truncates:
    # the next launch destroyed the log of the launch that had gone wrong, which was
    # noticed while trying to diagnose exactly such a launch. A log you overwrite
    # before reading is not a log.
    handle = log_path.open("ab")
    handle.write(f"\n===== launch {_timestamp()} :: {name} on port {port} =====\n".encode())
    handle.flush()

    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(script),
            "--server.port",
            str(port),
            "--server.address",
            "localhost",
            "--server.headless",
            "true",
            "--browser.gatherUsageStats",
            "false",
        ],
        cwd=str(PROJECT_ROOT),
        stdout=handle,
        stderr=subprocess.STDOUT,
        env={**os.environ, **(extra_env or {})},
    )
    return AppProcess(name=name, port=port, url=url, process=process, log_path=log_path)


def stop_app(app: AppProcess) -> None:
    """Stop an app, but only if we started it.

    `terminate` first so Streamlit can shut its sockets down, then `kill` if it
    will not go. A process left listening on 8501 would make the next launch
    silently reuse a dead-ish server.
    """
    if not app.ours or app.process is None:
        return
    if app.process.poll() is not None:
        return  # it already exited on its own

    app.process.terminate()
    try:
        app.process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        app.process.kill()
        app.process.wait(timeout=5)


@dataclass
class LaunchReport:
    """What happened, so the caller can print it instead of guessing."""

    apps: list[AppProcess] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    opened: list[str] = field(default_factory=list)


def open_in_browser(
    apps: list[AppProcess],
    gap: float = BROWSER_OPEN_GAP_S,
    opener: Callable[[str], object] = webbrowser.open,
    sleeper: Callable[[float], object] = time.sleep,
) -> list[str]:
    """Open each app in its own tab, one at a time. Returns the URLs attempted.

    Only used when both tabs are explicitly asked for; the default opens one. See
    `start_study_apps` for why.
    """
    opened: list[str] = []
    for index, app in enumerate(apps):
        if not port_is_serving(app.port):
            continue
        if index > 0:
            sleeper(gap)  # see BROWSER_OPEN_GAP_S
        opener(app.url)
        opened.append(app.url)
    return opened


def browser_targets(apps: list[AppProcess], open_all: bool = False) -> list[AppProcess]:
    """Which apps get a browser tab. By default, only the first: the study tool.

    Pulled out as its own function so the decision can be tested without starting
    two Streamlit servers to find out.
    """
    return apps if open_all else apps[:1]


def start_study_apps(
    ui_port: int = 8501,
    study_port: int = 8502,
    open_browser: bool = True,
    open_all: bool = False,
) -> LaunchReport:
    """Start the study interface and the app itself; open ONE browser tab.

    Why one tab, when two apps are started
    --------------------------------------
    Both servers run -- that part is wanted. But opening two tabs produced the
    reported failure: three windows, two of them showing the same app, and the
    study tool nowhere. The cause is that `webbrowser` on Windows is `os.startfile`,
    which hands a URL to the shell and returns immediately; fire two at a cold
    starting browser and the second can land on a window that is not ready for it.

    A gap between them makes that less likely. It does not make it impossible, and
    "less likely" is not a fix for something you meet every single time you sit
    down to study. So the default opens exactly ONE tab -- the study tool, which is
    what you came for -- and the app itself is one click away from inside it, plus
    printed in the terminal. One open cannot race with anything.

    `open_all=True` restores both tabs for anyone who prefers them.
    """
    package = Path(__file__).resolve().parent.parent
    wanted = [
        ("study-ui", package / "study" / "app.py", study_port),
        ("app-ui", package / "ui.py", ui_port),
    ]

    # The study app shows a link to the app itself, so one tab is enough. Passed by
    # environment because the port is only known here, at launch.
    app_url = f"http://localhost:{ui_port}"

    report = LaunchReport()
    for name, script, port in wanted:
        extra_env = {APP_URL_ENV: app_url} if name == "study-ui" else None
        app = start_app(name, script, port, extra_env=extra_env)
        report.apps.append(app)

        if app.reused:
            continue

        if not wait_until_serving(port):
            # Not fatal, and not hidden either.
            detail = f"{name} did not answer on port {port} within {STARTUP_TIMEOUT_S:.0f}s"
            if app.log_path is not None:
                detail += f" -- see {app.log_path}"
            report.failures.append(detail)
            continue

    if open_browser:
        report.opened = open_in_browser(browser_targets(report.apps, open_all=open_all))

    return report


def stop_study_apps(report: LaunchReport) -> None:
    """Stop every app we started. Anything reused is left alone."""
    for app in report.apps:
        stop_app(app)
