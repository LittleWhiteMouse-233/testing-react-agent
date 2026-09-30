"""Windows console owner for local development services.

Windows terminate()/tree-kill bypass ASGI shutdown. Use dedicated console
process groups and CTRL_BREAK, leaving Uvicorn and the MCP SDK in charge of
application cleanup. No application modules are imported by this launcher.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
from types import FrameType


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class AppLauncher:
    def __init__(self) -> None:
        self.processes: dict[str, subprocess.Popen[str]] = {}
        self.output_threads: list[threading.Thread] = []
        self.stop_requested = False
        self.force_requested = False
        self.shutdown_started = False

    def request_stop(self, signum: int, frame: FrameType | None) -> None:
        if not self.stop_requested:
            self.stop_requested = True
            print("[launcher] 正在停止服务… 等待后端保存结果和清理资源；再次 Ctrl+C 将强制退出。", flush=True)
        elif not self.force_requested:
            self.force_requested = True
            print("[launcher] 强制退出：结果可能未保存，资源清理可能未完成。", flush=True)

    def start(self, name: str, command: list[str], cwd: Path) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            command, cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        self.processes[name] = process
        print(f"[launcher] {name} process started (PID {process.pid}).", flush=True)

        def forward_output() -> None:
            assert process.stdout is not None
            with process.stdout:
                for line in process.stdout:
                    print(f"[{name}] {line}", end="", flush=True)

        output_thread = threading.Thread(target=forward_output, daemon=True)
        output_thread.start()
        self.output_threads.append(output_thread)
        return process

    def stop(self) -> None:
        if self.shutdown_started:
            return
        self.shutdown_started = True
        for process in self.processes.values():
            if process.poll() is None:
                try:
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                except OSError:
                    # A service can exit between polling and signal delivery.
                    if process.poll() is None:
                        raise

    def wait_for_shutdown(self) -> None:
        self.stop()
        while any(process.poll() is None for process in self.processes.values()):
            if self.force_requested:
                for process in self.processes.values():
                    if process.poll() is None:
                        subprocess.run(
                            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                            check=False, stdout=subprocess.DEVNULL,
                        )
                break
            time.sleep(0.1)
        for process in self.processes.values():
            process.wait()
        for output_thread in self.output_threads:
            output_thread.join()

    def run(self) -> int:
        node = shutil.which("node")
        if node is None:
            print("[launcher] FAIL: Node.js is unavailable.", flush=True)
            return 1
        try:
            print("[launcher] Updating the database...", flush=True)
            migration = self.start(
                "migration", [sys.executable, "-m", "alembic", "upgrade", "head"],
                PROJECT_ROOT / "backend",
            )
            while migration.poll() is None and not self.stop_requested:
                time.sleep(0.1)
            if self.stop_requested:
                return 130
            if migration.returncode != 0:
                print("[launcher] FAIL: Database migration failed; services were not started.", flush=True)
                return 1
            self.start("backend", [
                sys.executable, "-m", "uvicorn", "app.main:app",
                "--host", "127.0.0.1", "--port", "8000", "--workers", "1",
                "--timeout-graceful-shutdown", "3",
                "--log-config", str(PROJECT_ROOT / "backend/logging.json"),
            ], PROJECT_ROOT / "backend")
            if not self.stop_requested:
                self.start("frontend", [
                    node, str(PROJECT_ROOT / "frontend/node_modules/vite/bin/vite.js"),
                    "--host", "localhost", "--port", "5173", "--strictPort",
                ], PROJECT_ROOT / "frontend")
            print("[launcher] Starting http://localhost:5173 — Ctrl+C to stop both services.", flush=True)
            while not self.stop_requested:
                for name, process in self.processes.items():
                    if name != "migration" and process.poll() is not None:
                        print(f"[launcher] FAIL: {name} exited ({process.returncode}); stopping services.", flush=True)
                        return 1
                time.sleep(0.1)
            return 0
        except OSError as exc:
            print(f"[launcher] FAIL: {exc}", flush=True)
            return 1
        finally:
            self.wait_for_shutdown()


def main() -> int:
    parser = argparse.ArgumentParser(description="Start local development services.")
    parser.add_argument("--cmd-entry", action="store_true", help="Explain CMD confirmation after interruption.")
    arguments = parser.parse_args()
    if os.name != "nt":
        print("This launcher requires Windows.", flush=True)
        return 1
    launcher = AppLauncher()
    signal.signal(signal.SIGINT, launcher.request_stop)
    signal.signal(signal.SIGBREAK, launcher.request_stop)
    try:
        result = launcher.run()
        if launcher.force_requested:
            return 130
        print("[launcher] Services stopped.", flush=True)
        return result
    finally:
        if arguments.cmd_entry and launcher.stop_requested:
            print(
                "[launcher] 若随后出现“终止批处理操作吗(Y/N)?”：\n"
                "[launcher] 建议输入 N 并回车，让启动脚本完成目录恢复和退出码传递。\n"
                "[launcher] Y 会跳过剩余脚本收尾；两者都不会重新启动服务，也不会改变此前的关闭结果。",
                flush=True,
            )


if __name__ == "__main__":
    raise SystemExit(main())
