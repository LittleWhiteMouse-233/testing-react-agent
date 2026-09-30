"""Real Windows consoles, Uvicorn HTTP streams and independent stdio Fake MCP.

Each server owns a hidden console. A short-lived sender attaches only to that
console, so control events cannot interrupt pytest or the developer's terminal.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import ctypes
import os
from pathlib import Path
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator

import httpx
import pytest


ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows console shutdown")


@contextmanager
def console_process(command: list[str], log_path: Path, *, env: dict[str, str] | None = None,
                    interactive: bool = False) -> Iterator[subprocess.Popen[bytes]]:
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    with log_path.open("wb") as output:
        process = subprocess.Popen(
            command, cwd=ROOT / "backend", env=env,
            stdin=subprocess.PIPE if interactive else subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=startup,
        )
        try:
            yield process
        finally:
            if process.poll() is None:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
                process.wait(timeout=10)
            if process.stdin is not None:
                process.stdin.close()


def console_signal(process: subprocess.Popen[bytes], event: int = 1) -> None:
    # Group zero is safe here: this helper attaches to a dedicated test console.
    sender = (
        "import ctypes,time; k=ctypes.windll.kernel32; k.FreeConsole(); "
        f"assert k.AttachConsole({process.pid}), ctypes.WinError(); "
        "assert k.SetConsoleCtrlHandler(None, True); "
        f"assert k.GenerateConsoleCtrlEvent({event}, 0); time.sleep(0.1)"
    )
    result = subprocess.run([sys.executable, "-c", sender], creationflags=subprocess.CREATE_NO_WINDOW,
                            capture_output=True, timeout=10)
    # CTRL_BREAK also terminates the sender after delivering the event.
    assert result.returncode in (0, 0xC000013A, -1073741510), result.stderr


def process_alive(pid: int) -> bool:
    kernel = ctypes.windll.kernel32
    kernel.OpenProcess.restype = ctypes.c_void_p
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    exit_code = ctypes.c_ulong()
    try:
        assert kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(exit_code))
        return exit_code.value == 259
    finally:
        kernel.CloseHandle(ctypes.c_void_p(handle))


def wait_for_text(path: Path, text: str, process: subprocess.Popen[bytes], timeout: float = 25) -> str:
    deadline = time.monotonic() + timeout
    output = ""
    while time.monotonic() < deadline:
        output = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        if text in output:
            return output
        assert process.poll() is None, output
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for {text!r} in {path}: {output}")


def test_cmd_entry_ctrl_c_explains_confirmation_and_releases_ports(tmp_path: Path) -> None:
    env = {**os.environ, "TEST_AGENT_DATA_DIR": str(tmp_path),
           "TEST_AGENT_DATABASE_URL": f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}",
           "TEST_AGENT_CHECKPOINT_PATH": str(tmp_path / "checkpoints.db")}
    log = tmp_path / "entry.log"
    command = [os.environ["COMSPEC"], "/d", "/c", str(ROOT / "start.cmd")]
    with console_process(command, log, env=env, interactive=True) as process:
        wait_for_text(log, "Application startup complete", process, timeout=40)
        wait_for_text(log, "Local:", process)
        console_signal(process, event=0)
        wait_for_text(log, "正在停止服务", process, timeout=3)
        wait_for_text(log, "Services stopped", process)
        wait_for_text(log, "建议输入 N", process)
        assert process.stdin is not None
        process.stdin.write(b"N\r\n")
        process.stdin.flush()
        process.wait(timeout=20)
    output = log.read_text(encoding="utf-8", errors="replace")
    assert "Application shutdown completed" in output, output
    assert output.count("建议输入 N") == 1
    assert output.index("Services stopped") < output.index("建议输入 N")
    assert all(not process_alive(int(pid)) for pid in re.findall(r"process started \(PID (\d+)\)", output))
    for family, host, port in [(socket.AF_INET, "127.0.0.1", 8000),
                               (socket.AF_INET, "127.0.0.1", 5173), (socket.AF_INET6, "::1", 5173)]:
        with socket.socket(family) as probe:
            assert probe.connect_ex((host, port)) != 0


def test_second_ctrl_c_forces_only_owned_children(tmp_path: Path) -> None:
    log = tmp_path / "force.log"
    with console_process([sys.executable, str(Path(__file__).resolve()), "launcher", str(tmp_path)], log) as process:
        wait_for_text(log, "child ready", process)
        child_pid = int((tmp_path / "child.pid").read_text())
        console_signal(process, event=0)
        wait_for_text(log, "child stopping", process)
        assert process_alive(child_pid)
        console_signal(process, event=0)
        process.wait(timeout=15)
        assert process.returncode == 130
        assert not process_alive(child_pid)
    output = log.read_text(encoding="utf-8", errors="replace")
    assert "强制退出：结果可能未保存" in output
    assert "Terminate batch job" not in output


@pytest.mark.parametrize("cmd_entry,stop_count,exit_code", [
    (True, 0, 1), (False, 1, 0), (True, 1, 0), (True, 2, 130),
])
def test_launcher_confirmation_matches_entry_and_stop_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    cmd_entry: bool, stop_count: int, exit_code: int,
) -> None:
    from scripts import start_app

    def finish_run(launcher: start_app.AppLauncher) -> int:
        for _ in range(stop_count):
            launcher.request_stop(signal.SIGINT, None)
        return 1 if stop_count == 0 else 0

    monkeypatch.setattr(sys, "argv", ["start_app.py", *(["--cmd-entry"] if cmd_entry else [])])
    monkeypatch.setattr(start_app.signal, "signal", lambda *_: None)
    monkeypatch.setattr(start_app.AppLauncher, "run", finish_run)
    assert start_app.main() == exit_code
    output = capsys.readouterr().out
    assert output.count("建议输入 N") == int(cmd_entry and stop_count > 0)
    if stop_count == 2:
        assert "Services stopped" not in output


def test_launcher_confirmation_does_not_hide_shutdown_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts import start_app

    def fail_shutdown(launcher: start_app.AppLauncher) -> int:
        launcher.request_stop(signal.SIGINT, None)
        raise OSError("signal delivery failed")

    monkeypatch.setattr(sys, "argv", ["start_app.py", "--cmd-entry"])
    monkeypatch.setattr(start_app.signal, "signal", lambda *_: None)
    monkeypatch.setattr(start_app.AppLauncher, "run", fail_shutdown)
    with pytest.raises(OSError, match="signal delivery failed"):
        start_app.main()
    output = capsys.readouterr().out
    assert output.count("建议输入 N") == 1
    assert "Services stopped" not in output


def test_failed_migration_never_starts_services(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.start_app import AppLauncher
    launcher = AppLauncher()
    original_start = launcher.start
    started: list[str] = []

    def start_failed_migration(name: str, command: list[str], cwd: Path) -> subprocess.Popen[str]:
        started.append(name)
        return original_start(name, [sys.executable, "-c", "raise SystemExit(7)"], tmp_path)

    monkeypatch.setattr(launcher, "start", start_failed_migration)
    assert launcher.run() == 1
    assert started == ["migration"]


@pytest.mark.parametrize("service", ["backend", "frontend"])
def test_service_port_conflict_stops_peer(tmp_path: Path, service: str) -> None:
    env = {**os.environ, "TEST_AGENT_DATA_DIR": str(tmp_path),
           "TEST_AGENT_DATABASE_URL": f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}",
           "TEST_AGENT_CHECKPOINT_PATH": str(tmp_path / "checkpoints.db")}
    log = tmp_path / "conflict.log"
    with ExitStack() as sockets:
        addresses = [(socket.AF_INET, "127.0.0.1", 8000)] if service == "backend" else [
            (socket.AF_INET, "127.0.0.1", 5173), (socket.AF_INET6, "::1", 5173),
        ]
        # localhost may resolve to either loopback family on the developer's machine.
        for family, host, port in addresses:
            occupied = sockets.enter_context(socket.socket(family))
            occupied.bind((host, port))
            occupied.listen()
        with console_process([sys.executable, str(ROOT / "backend/scripts/start_app.py")], log, env=env) as process:
            process.wait(timeout=30)
            assert process.returncode == 1
        output = log.read_text(encoding="utf-8", errors="replace")
        assert f"{service} exited" in output, output
        assert all(not process_alive(int(pid)) for pid in re.findall(r"process started \(PID (\d+)\)", output))


@pytest.mark.parametrize("phase", ["idle", "tool", "model", "cleanup"])
def test_uvicorn_shutdown_with_open_sse_saves_run_and_closes_mcp(tmp_path: Path, phase: str) -> None:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    log = tmp_path / "server.log"
    with console_process([sys.executable, str(Path(__file__).resolve()), "server", str(tmp_path), str(port), phase], log) as process:
        wait_for_text(log, "Application startup complete", process)
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=15, trust_env=False) as client:
            from mcp_support import create_plan
            plan = create_plan(client)  # type: ignore[arg-type]
            run_id: str | None = None
            if phase != "idle":
                response = client.post("/api/runs", json={"test_plan_id": plan["id"], "assumptions_confirmed": True})
                assert response.status_code == 201, response.text
                run_id = response.json()["id"]
                marker = tmp_path / ("fixture.jsonl" if phase == "tool" else "phase.txt")
                wait_for_text(marker, '"started"' if phase == "tool" else phase, process)
            stream_path = (f"/api/runs/{run_id}/stream" if phase == "tool" else
                           f"/api/test-cases/{plan['test_case_id']}/planning/stream")
            with client.stream("GET", stream_path) as response:
                assert response.status_code == 200, response.text
                next(response.iter_lines())
                console_signal(process)
                process.wait(timeout=25)
        output = log.read_text(encoding="utf-8", errors="replace")
        assert "Application shutdown completed" in output, output
        assert "Terminate batch job" not in output
        if run_id:
            with sqlite3.connect(tmp_path / "app.db") as connection:
                status, verdict = connection.execute("select status, verdict from test_runs where id = ?", (run_id,)).fetchone()
                assert status == "finished" and verdict == ("PASS" if phase == "cleanup" else "CANCELLED"), output
                event_types = [row[0] for row in connection.execute(
                    "select type from run_events where test_run_id = ? order by sequence", (run_id,),
                )]
                terminal = "run.finished" if phase == "cleanup" else "run.cancelled"
                assert event_types[-1] == terminal and event_types.count(terminal) == 1
                assert "execution.error" not in event_types
            from mcp_support import read_records
            records = read_records(tmp_path)
            assert any(record["phase"] == "closed" for record in records), records
            assert all(not process_alive(record["pid"]) for record in records)


def run_server(root: Path, port: int, phase: str) -> None:
    import asyncio
    from contextlib import asynccontextmanager
    from typing import cast
    from fastapi import FastAPI
    import uvicorn
    from app.container import Container
    from app.llm import ScriptedChatModel
    from mcp_support import build_client, replay_call

    client = build_client(root, calls=[replay_call(delay=60 if phase == "tool" else 0)])
    app = cast(FastAPI, client.app)
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(instance: FastAPI):
        async with original_lifespan(instance):
            container = cast(Container, instance.state.container)
            if phase == "model":
                from langchain_core.runnables import RunnableLambda
                original_bind = ScriptedChatModel.bind_tools

                def delayed_bind(self, *args, **kwargs):
                    bound = original_bind(self, *args, **kwargs)

                    async def invoke(messages):
                        (root / "phase.txt").write_text("model")
                        await asyncio.sleep(8)
                        return await bound.ainvoke(messages)

                    return RunnableLambda(invoke)

                ScriptedChatModel.bind_tools = delayed_bind
            if phase == "cleanup":
                original_connect = container.tools.connect

                @asynccontextmanager
                async def delayed_cleanup(**kwargs):
                    async with original_connect(**kwargs) as tools:
                        yield tools
                        (root / "phase.txt").write_text("cleanup")
                        await asyncio.sleep(5)

                container.tools.connect = delayed_cleanup
            yield

    app.router.lifespan_context = lifespan
    uvicorn.run(app, host="127.0.0.1", port=port, timeout_graceful_shutdown=3,
                log_config=str(ROOT / "backend/logging.json"))


if __name__ == "__main__":
    if sys.argv[1] == "server":
        run_server(Path(sys.argv[2]), int(sys.argv[3]), sys.argv[4])
    else:
        sys.path.insert(0, str(ROOT / "backend"))
        from scripts.start_app import AppLauncher
        launcher = AppLauncher()
        signal.signal(signal.SIGINT, launcher.request_stop)
        signal.signal(signal.SIGBREAK, launcher.request_stop)
        child = launcher.start("backend", [sys.executable, "-u", "-c",
            "import signal,time; signal.signal(signal.SIGBREAK, lambda *_: print('child stopping', flush=True)); "
            "print('child ready', flush=True)\nwhile True:\n time.sleep(0.1)"], Path(sys.argv[2]))
        (Path(sys.argv[2]) / "child.pid").write_text(str(child.pid))
        while not launcher.stop_requested:
            time.sleep(0.05)
        launcher.wait_for_shutdown()
        raise SystemExit(130 if launcher.force_requested else 0)
