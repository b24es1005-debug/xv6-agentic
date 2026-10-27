#!/usr/bin/env python3
"""Deterministic, read-only host agent for an xv6 guest running via `make qemu`."""

from __future__ import annotations

import os
import re
import secrets
import select
import subprocess
import time
from dataclasses import dataclass
from typing import Any


EXERCISE_DIR = "/"
GUEST_EXERCISE_DIR = ""
PROMPT = b"$ "
MAX_OUTPUT_BYTES = 64 * 1024

# Paths are deliberately restricted to one simple filename directly under
# the guest root (/). This prevents traversal, shell metacharacters, and nested paths.
# This root-level configuration is for basic bridge testing; create a dedicated
# /exercise directory in the guest image before switching to that isolated layout.
_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


class ToolError(Exception):
    """An invalid tool request or failed guest operation."""


@dataclass(frozen=True)
class ToolCall:
    tool: str
    args: dict[str, Any]


class QemuSerialBridge:
    """Run make qemu and exchange commands with the xv6 serial console."""

    def __init__(self, *, startup_timeout: float = 30.0, command_timeout: float = 10.0):
        self.command_timeout = command_timeout
        self.proc = subprocess.Popen(
            ["make", "qemu"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            # Capture build/QEMU diagnostics with the console stream. Startup
            # consumes through the xv6 prompt before any tool output is parsed.
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
        if self.proc.stdin is None or self.proc.stdout is None:
            self.close()
            raise ToolError("Could not open QEMU console pipes")

        self._buffer = bytearray()
        try:
            self._read_until(PROMPT, startup_timeout)
        except Exception:
            self.close()
            raise

    def _read_until(self, marker: bytes, timeout: float) -> bytes:
        """Read in chunks until marker arrives; preserve bytes after the marker."""
        deadline = time.monotonic() + timeout

        while True:
            marker_at = self._buffer.find(marker)
            if marker_at >= 0:
                end = marker_at + len(marker)
                result = bytes(self._buffer[:end])
                del self._buffer[:end]
                return result

            if len(self._buffer) > MAX_OUTPUT_BYTES:
                raise ToolError("Guest output exceeded the configured limit")

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ToolError("Timed out waiting for the xv6 shell")

            if self.proc.poll() is not None:
                raise ToolError("QEMU exited while waiting for guest output")

            readable, _, _ = select.select([self.proc.stdout], [], [], remaining)
            if not readable:
                continue

            chunk = os.read(self.proc.stdout.fileno(), 4096)
            if not chunk:
                raise ToolError("QEMU closed the serial console")
            self._buffer.extend(chunk)

    def run_tool(self, tool: str, filename: str | None = None) -> str:
        """Submit only commands built from this bridge's fixed tool allowlist."""
        if tool == "list_files" and filename is None:
            fixed_command = "ls"
        elif tool == "read_file" and isinstance(filename, str) and _FILENAME.fullmatch(filename):
            fixed_command = f"cat {filename}"
        else:
            raise ToolError("Bridge refused an invalid tool or filename")

        # The marker is restricted to hexadecimal characters.
        marker = ("__PY_DONE_" + secrets.token_hex(8) + "__").encode("ascii")
        line = fixed_command.encode("ascii") + b"; echo " + marker + b"\n"

        if self.proc.poll() is not None:
            raise ToolError("QEMU is not running")

        try:
            self.proc.stdin.write(line)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise ToolError("Could not write to the QEMU console") from exc

        response = self._read_until(marker, self.command_timeout)
        # Consume the shell's next prompt too, so the next request starts from
        # a known command boundary. This may require additional serial reads.
        response += self._read_until(PROMPT, self.command_timeout)
        text = response.decode("utf-8", errors="replace")

        # Drop the echoed command and the marker line; retain guest output.
        lines = text.splitlines()
        if lines and fixed_command in lines[0]:
            lines = lines[1:]
        cleaned = []
        for line_text in lines:
            if marker.decode("ascii") in line_text:
                break
            cleaned.append(line_text)
        return "\n".join(cleaned).strip()

    def close(self) -> None:
        if getattr(self, "proc", None) is None:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc = None

    def __enter__(self) -> "QemuSerialBridge":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class ToolDispatcher:
    """Validate structured calls and map them to approved guest commands."""

    # Exact schemas: no extra arguments are accepted.
    SCHEMAS = {
        "list_files": {},
        "read_file": {"path": str},
    }

    def __init__(self, bridge: QemuSerialBridge):
        self.bridge = bridge

    def validate(self, call: ToolCall) -> None:
        if not isinstance(call, ToolCall):
            raise ToolError("Tool call must be a ToolCall object")
        if call.tool not in self.SCHEMAS:
            raise ToolError(f"Unknown tool: {call.tool!r}")
        if not isinstance(call.args, dict):
            raise ToolError("Tool arguments must be an object")

        schema = self.SCHEMAS[call.tool]
        if set(call.args) != set(schema):
            expected = ", ".join(schema) if schema else "no arguments"
            raise ToolError(f"{call.tool} requires exactly: {expected}")

        for name, expected_type in schema.items():
            value = call.args[name]
            # `type(...) is ...` rejects bool where an integer might be expected.
            if type(value) is not expected_type:
                raise ToolError(f"Argument {name!r} must be a {expected_type.__name__}")

        if call.tool == "read_file":
            path = call.args["path"]
            prefix = "/"
            if not path.startswith(prefix):
                raise ToolError("Path must be an absolute path under /")
            filename = path[len(prefix):]
            if not _FILENAME.fullmatch(filename) or filename in {".", ".."}:
                raise ToolError("Path must name one simple file directly under /")

    def execute(self, call: ToolCall) -> str:
        self.validate(call)

        if call.tool == "list_files":
            return self.bridge.run_tool("list_files")
        elif call.tool == "read_file":
            # This value has passed the strict filename allowlist above.
            filename = call.args["path"].split("/")[-1]
            return self.bridge.run_tool("read_file", filename)
        else:
            # Defensive guard in case the allowlist is changed incorrectly.
            raise ToolError(f"Unknown tool: {call.tool!r}")



class DeterministicAgent:
    """Map a small set of request prefixes to validated tools, without an LLM."""

    def __init__(self, tools: ToolDispatcher):
        self.tools = tools

    def run(self, request: str) -> str:
        if not isinstance(request, str):
            raise ToolError("Request must be text")

        normalized = request.strip()
        lowered = normalized.lower()

        if lowered == "list" or lowered.startswith("list "):
            call = ToolCall("list_files", {})
        elif lowered.startswith("read "):
            requested_path = normalized[len("read "):].strip()
            call = ToolCall("read_file", {"path": requested_path})
        else:
            raise ToolError("Unsupported request. Try 'list' or 'read /NAME'.")

        return self.tools.execute(call)


def main() -> int:
    try:
        with QemuSerialBridge() as bridge:
            agent = DeterministicAgent(ToolDispatcher(bridge))
            print("Agent ready. Enter 'list', 'read /NAME', or 'quit'.")

            while True:
                try:
                    request = input("> ")
                except EOFError:
                    break

                if request.strip().lower() in {"quit", "exit"}:
                    break

                try:
                    observation = agent.run(request)
                    print(observation or "(no output)")
                except ToolError as exc:
                    print(f"Request rejected or failed: {exc}")

    except ToolError as exc:
        print(f"Could not start QEMU bridge: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
