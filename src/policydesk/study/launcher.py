"""Starting the two web apps alongside the terminal tutor.

Why this exists
---------------
The desktop icon used to open the tutor and nothing else, so the two Streamlit
apps had to be started by hand in two more terminals. That is three things to
remember before you can study, which is three chances to not bother.

So the icon now opens everything: the study interface, the app itself, and the
tutor in the window you clicked.

The three rules this module follows
----------------------------------
1. **Reuse a port that is already serving.** Double-clicking the icon twice, or
   having run `policydesk study` earlier, must not fail and must not start a
   second copy. If something is already answering on the port, we use it.

2. **Only stop what we started.** If you had a server running already, it is
   still running when the tutor closes. Terminating a process this program did
   not create would be a surprising thing for a learning tool to do.

3. **A web app that will not start must not block the tutor.** The tutor is the
   point; the browser tabs are a convenience. A failure here is printed, with the
   log path, and then we carry on -- never swallowed, never fatal.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
LOG_DIR = PROJECT_ROOT / "logs"

# How long to wait for Streamlit to answer before giving up on it. Streamlit's
# first start is the slow one: it imports torch-sized dependency trees and builds
# its static assets cache.
STARTUP_TIMEOUT_S = 60.0


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


def start_app(name: str, script: Path, port: int) -> AppProcess:
    """Start one Streamlit app, or adopt the one already on that port."""
    url = f"http://localhost:{port}"

    if port_is_serving(port):
        return AppProcess(name=name, port=port, url=url, reused=True)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{name}.log"

    # Streamlit's own output goes to a file rather than into the tutor's window,
    # where it would scroll the lesson away. The path is printed, so a failure is
    # still followable -- the output is redirected, not discarded.
    handle = log_path.open("wb")
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


def start_study_apps(ui_port: int = 8501, study_port: int = 8502) -> LaunchReport:
    """Start the study interface and the app itself, and open both in a browser."""
    package = Path(__file__).resolve().parent.parent
    wanted = [
        ("study-ui", package / "study" / "app.py", study_port),
        ("app-ui", package / "ui.py", ui_port),
    ]

    report = LaunchReport()
    for name, script, port in wanted:
        app = start_app(name, script, port)
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

    for app in report.apps:
        if port_is_serving(app.port):
            webbrowser.open(app.url)

    return report


def stop_study_apps(report: LaunchReport) -> None:
    """Stop every app we started. Anything reused is left alone."""
    for app in report.apps:
        stop_app(app)
