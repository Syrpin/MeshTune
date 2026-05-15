from __future__ import annotations

import asyncio
import csv
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class CommandResult:
    command: str
    response_lines: list[str]
    payload_lines: list[str]
    ok: bool
    raw: str


class AD5XPrinter:
    def __init__(self, host: str, port: int, temp_csv_path: Path, temp_json_path: Path):
        self.host = host
        self.port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

        self.connected = False
        self._connect_lock = asyncio.Lock()

        self._queue: asyncio.Queue[tuple[str, asyncio.Future[CommandResult]]] = asyncio.Queue()
        self._worker_task: asyncio.Task[None] | None = None

        self.temp_csv_path = temp_csv_path
        self.temp_json_path = temp_json_path

        self.cache: dict[str, Any] = {
            "temperatures": {
                "nozzle": None,
                "target_nozzle": None,
                "bed": None,
                "target_bed": None,
            },
            "position": {"x": None, "y": None, "z": None, "e": None},
            "endstops": {"x": "unknown", "y": "unknown", "z": "unknown"},
            "print_status": {"state": "unknown", "bytes": None, "bytes_total": None, "layer": None, "layer_total": None},
            "identity": {"machine": None, "firmware": None, "sn": None},
            "protocol": {
                "m114_payload_available": False,
                "last_command": None,
                "last_raw_response": None,
            },
            "bed_mesh": None,
            "last_update": None,
        }

    async def start(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._queue_worker())

    async def stop(self) -> None:
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        await self.disconnect()

    async def connect(self) -> bool:
        async with self._connect_lock:
            if self.connected:
                return True
            try:
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port), timeout=5.0
                )
                self.connected = True
                logger.info("Connected to AD5X at %s:%s", self.host, self.port)
                return True
            except Exception as exc:
                self.connected = False
                logger.warning("Connection failed %s:%s: %s", self.host, self.port, exc)
                return False

    async def disconnect(self) -> None:
        self.connected = False
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = None
        self._writer = None

    async def send_command(self, command: str, timeout: float = 3.0) -> CommandResult:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[CommandResult] = loop.create_future()
        await self._queue.put((command, fut))
        return await asyncio.wait_for(fut, timeout=timeout + 1.0)

    async def _queue_worker(self) -> None:
        while True:
            command, fut = await self._queue.get()
            try:
                result = await self._send_command_direct(command)
                if not fut.done():
                    fut.set_result(result)
            except Exception as exc:
                if not fut.done():
                    fut.set_result(CommandResult(command=command, response_lines=[], payload_lines=[], ok=False, raw=str(exc)))
            finally:
                self._queue.task_done()

    async def _send_command_direct(self, command: str, timeout: float = 3.0) -> CommandResult:
        if not self.connected:
            ok = await self.connect()
            if not ok:
                return CommandResult(command=command, response_lines=[], payload_lines=[], ok=False, raw="connect_failed")

        assert self._reader is not None
        assert self._writer is not None

        full_cmd = f"~{command.strip()}\r\n"
        self._writer.write(full_cmd.encode("utf-8"))
        await self._writer.drain()

        response_lines: list[str] = []
        try:
            while True:
                line = await asyncio.wait_for(self._reader.readline(), timeout=timeout)
                if not line:
                    break
                text = line.decode("utf-8", errors="ignore").strip()
                if not text:
                    continue
                response_lines.append(text)
                if text.lower() == "ok":
                    break
        except asyncio.TimeoutError:
            pass
        except Exception:
            await self.disconnect()
            return CommandResult(command=command, response_lines=response_lines, payload_lines=[], ok=False, raw="io_error")

        cmd_prefix = f"CMD {command.split()[0].upper()}"
        payload_lines = [ln for ln in response_lines if not ln.startswith(cmd_prefix) and ln.lower() != "ok"]
        raw = "\n".join(response_lines)
        ok = any(ln.lower() == "ok" for ln in response_lines) or bool(response_lines)

        self.cache["protocol"]["last_command"] = command
        self.cache["protocol"]["last_raw_response"] = raw

        return CommandResult(command=command, response_lines=response_lines, payload_lines=payload_lines, ok=ok, raw=raw)

    @staticmethod
    def _extract_floats(line: str) -> dict[str, float]:
        return {k.lower(): float(v) for k, v in re.findall(r"([A-Za-z]):\s*(-?\d+(?:\.\d+)?)", line)}

    def _parse_m105(self, result: CommandResult) -> None:
        payload = " ".join(result.payload_lines or result.response_lines)
        # Accept both T: and T0: forms used by Flashforge firmware.
        nozzle_match = re.search(r"T(?:0)?:\s*(-?\d+(?:\.\d+)?)\s*/\s*(-?\d+(?:\.\d+)?)", payload)
        bed_match = re.search(r"B:\s*(-?\d+(?:\.\d+)?)\s*/\s*(-?\d+(?:\.\d+)?)", payload)

        if nozzle_match:
            self.cache["temperatures"]["nozzle"] = float(nozzle_match.group(1))
            self.cache["temperatures"]["target_nozzle"] = float(nozzle_match.group(2))
        if bed_match:
            self.cache["temperatures"]["bed"] = float(bed_match.group(1))
            self.cache["temperatures"]["target_bed"] = float(bed_match.group(2))

    def _parse_m114(self, result: CommandResult) -> None:
        payload = " ".join(result.payload_lines)
        if not payload:
            self.cache["protocol"]["m114_payload_available"] = False
            return

        vals = self._extract_floats(payload)
        for axis in ("x", "y", "z", "e"):
            if axis in vals:
                self.cache["position"][axis] = vals[axis]
        self.cache["protocol"]["m114_payload_available"] = True

    def _parse_m119(self, result: CommandResult) -> None:
        payload = "\n".join(result.payload_lines or result.response_lines)

        for axis in ("x", "y", "z"):
            trig = re.search(rf"{axis}(?:-min|-max)?\s*:\s*(\d+|open|triggered)", payload, flags=re.IGNORECASE)
            if trig:
                token = trig.group(1).lower()
                if token in {"1", "triggered"}:
                    self.cache["endstops"][axis] = "triggered"
                else:
                    self.cache["endstops"][axis] = "open"

        if "machinestatus" in payload.lower():
            st = re.search(r"MachineStatus\s*:\s*([A-Za-z_]+)", payload, flags=re.IGNORECASE)
            if st:
                self.cache["print_status"]["state"] = st.group(1).lower()

    def _parse_m27(self, result: CommandResult) -> None:
        payload = "\n".join(result.payload_lines or result.response_lines)
        m_bytes = re.search(r"SD printing byte\s+(\d+)\s*/\s*(\d+)", payload, flags=re.IGNORECASE)
        m_layer = re.search(r"Layer\s*:\s*(\d+)\s*/\s*(\d+)", payload, flags=re.IGNORECASE)

        if m_bytes:
            self.cache["print_status"]["bytes"] = int(m_bytes.group(1))
            self.cache["print_status"]["bytes_total"] = int(m_bytes.group(2))
            total = int(m_bytes.group(2))
            done = int(m_bytes.group(1))
            if total > 0 and done > 0:
                self.cache["print_status"]["state"] = "printing"

        if m_layer:
            self.cache["print_status"]["layer"] = int(m_layer.group(1))
            self.cache["print_status"]["layer_total"] = int(m_layer.group(2))

    def _parse_m115(self, result: CommandResult) -> None:
        payload = "\n".join(result.payload_lines or result.response_lines)
        machine = re.search(r"Machine\s+Type\s*:\s*(.+)", payload, flags=re.IGNORECASE)
        fw = re.search(r"Firmware\s*:\s*(.+)", payload, flags=re.IGNORECASE)
        sn = re.search(r"SN\s*:\s*(.+)", payload, flags=re.IGNORECASE)

        if machine:
            self.cache["identity"]["machine"] = machine.group(1).strip()
        if fw:
            self.cache["identity"]["firmware"] = fw.group(1).strip()
        if sn:
            self.cache["identity"]["sn"] = sn.group(1).strip()

    def _persist_temperature_log(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        rec = {
            "timestamp": now,
            "nozzle": self.cache["temperatures"]["nozzle"],
            "target_nozzle": self.cache["temperatures"]["target_nozzle"],
            "bed": self.cache["temperatures"]["bed"],
            "target_bed": self.cache["temperatures"]["target_bed"],
        }

        self.temp_csv_path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.temp_csv_path.exists()
        with self.temp_csv_path.open("a", newline="", encoding="utf-8") as fp:
            writer = csv.DictWriter(fp, fieldnames=list(rec.keys()))
            if new_file:
                writer.writeheader()
            writer.writerow(rec)

        self.temp_json_path.write_text(json.dumps(rec, ensure_ascii=True), encoding="utf-8")

    async def update_all(self) -> dict[str, Any]:
        m105 = await self.send_command("M105")
        self._parse_m105(m105)

        m114 = await self.send_command("M114")
        self._parse_m114(m114)

        m119 = await self.send_command("M119")
        self._parse_m119(m119)

        m27 = await self.send_command("M27")
        self._parse_m27(m27)

        if self.cache["identity"]["machine"] is None:
            m115 = await self.send_command("M115")
            self._parse_m115(m115)

        self.cache["last_update"] = datetime.now(timezone.utc).isoformat()
        self._persist_temperature_log()
        return self.cache

    def get_cache(self) -> dict[str, Any]:
        return self.cache
