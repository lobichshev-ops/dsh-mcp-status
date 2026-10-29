#!/usr/bin/env python
"""Проверка живости MCP-серверов, подключённых к DSH.

Что делает:

1. читает конфиг профиля (~/.dsh/profiles/<профиль>/cordis.patch.yml) и
   вытаскивает все записи @deepseek-ai/dsh-mcp-client — это ровно тот список,
   который DSH пробует подключить при старте;
2. по каждому серверу выполняет настоящий MCP-хендшейк (initialize +
   tools/list) его транспортом и считает инструменты;
3. отдельно проверяет инстансы 1C:EDT из .mcp.json в папках проектов;
4. печатает таблицу и короткий итог.

Только стандартная библиотека, никаких зависимостей и сети наружу — все
запросы идут на localhost, к тем адресам, что уже прописаны в конфиге.

Запуск:
    python mcp_status.py                 # таблица
    python mcp_status.py --json          # машинный вывод
    python mcp_status.py --timeout 3     # быстрее на мёртвых серверах
    python mcp_status.py --no-edt        # без блока EDT-инстансов
    python mcp_status.py --no-scan       # без сканирования диапазона портов EDT
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from queue import Empty, Queue

MCP_PLUGIN = "@deepseek-ai/dsh-mcp-client"
PROTOCOL = "2025-06-18"
CLIENT_INFO = {"name": "dsh-mcp-status", "version": "1.0"}

STATE_LABEL = {
    "ok": "OK",
    "warn": "ЧАСТИЧНО",
    "dead": "МЁРТВ",
    "off": "ВЫКЛ",
    "skipped": "НЕ ПРОВЕРЕН",
}

TRANSPORT_LABEL = {
    "streamable-http": "http",
    "http": "http",
    "sse": "sse",
    "stdio": "stdio",
}


# --------------------------------------------------------------------------
# минимальный разбор YAML: нужны только блоки-словари и блоки-списки
# --------------------------------------------------------------------------

def _strip_comment(line: str) -> str:
    quote = None
    for index, char in enumerate(line):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in "'\"":
            quote = char
        elif char == "#" and (index == 0 or line[index - 1] in " \t"):
            return line[:index]
    return line


def _scalar(value: str):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    if value in ("", "~", "null", "Null", "NULL"):
        return None
    if value in ("true", "True", "yes"):
        return True
    if value in ("false", "False", "no"):
        return False
    if value == "[]":
        return []
    if value == "{}":
        return {}
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


class _YamlParser:
    """Разбор подмножества YAML: блоки-словари, блоки-списки, скаляры."""

    def __init__(self, lines):
        self.lines = list(lines)
        self.pos = 0

    @staticmethod
    def _indent(line: str) -> int:
        return len(line) - len(line.lstrip(" "))

    def parse(self):
        if not self.lines:
            return []
        return self._block(self._indent(self.lines[0]))

    def _block(self, indent):
        if self.lines[self.pos].lstrip().startswith("- "):
            return self._seq(indent)
        return self._map(indent)

    def _map(self, indent):
        result = {}
        while self.pos < len(self.lines):
            line = self.lines[self.pos]
            if self._indent(line) != indent:
                break
            body = line.strip()
            if body.startswith("- "):
                break
            match = re.match(r"([A-Za-z_][\w.\-]*):(?:\s+(.*))?$", body)
            if not match:
                self.pos += 1
                continue
            key, value = match.group(1), (match.group(2) or "").strip()
            self.pos += 1
            if value:
                result[key] = _scalar(value)
            elif self.pos < len(self.lines) and self._indent(self.lines[self.pos]) > indent:
                result[key] = self._block(self._indent(self.lines[self.pos]))
            else:
                result[key] = None
        return result

    def _seq(self, indent):
        result = []
        while self.pos < len(self.lines):
            line = self.lines[self.pos]
            if self._indent(line) != indent:
                break
            body = line.strip()
            if not body.startswith("- "):
                break
            rest = body[2:].strip()
            if not rest:
                self.pos += 1
                result.append(None)
            elif re.match(r"[A-Za-z_][\w.\-]*:", rest):
                # "- key: value" — начало словаря на два пробела глубже
                self.lines[self.pos] = " " * (indent + 2) + rest
                result.append(self._map(indent + 2))
            else:
                self.pos += 1
                result.append(_scalar(rest))
        return result


def parse_yaml(text: str):
    lines = []
    for raw in text.splitlines():
        cleaned = _strip_comment(raw).rstrip()
        if cleaned.strip():
            lines.append(cleaned)
    return _YamlParser(lines).parse() if lines else []


# --------------------------------------------------------------------------
# конфиг профиля
# --------------------------------------------------------------------------

def default_profile_dir() -> str:
    home = os.path.expanduser("~")
    base = os.path.join(home, ".dsh", "profiles")
    if os.path.isdir(base):
        entries = [name for name in os.listdir(base) if os.path.isdir(os.path.join(base, name))]
        if "desktop" in entries:
            return os.path.join(base, "desktop")
        if entries:
            return os.path.join(base, sorted(entries)[0])
    return os.path.join(base, "desktop")


def _collect_entries(node, acc):
    if isinstance(node, list):
        for item in node:
            _collect_entries(item, acc)
    elif isinstance(node, dict):
        if node.get("name") == MCP_PLUGIN:
            acc.append(node)
            return
        for value in node.values():
            _collect_entries(value, acc)


def _arg_value(args, flag):
    try:
        return args[args.index(flag) + 1]
    except (ValueError, IndexError):
        return None


def describe_server(entry):
    config = entry.get("config") or {}
    name = config.get("serverName") or entry.get("id") or "?"
    transport = str(config.get("transport") or "stdio")
    disabled = bool(entry.get("disabled")) or bool(config.get("disabled"))
    args = [str(item) for item in (config.get("args") or [])]
    url = config.get("url") or ""
    if transport in ("http", "streamable-http", "sse"):
        endpoint = url
    else:
        command = config.get("command") or ""
        script = next((item for item in args if item.lower().endswith((".py", ".js", ".mjs", ".exe"))), "")
        parts = [os.path.basename(command)] if command else []
        if script:
            parts.append(os.path.basename(script))
        parts.extend(item for item in args if item not in (script,) and not item.startswith("--") and not item.startswith("E:\\"))
        endpoint = " ".join(part for part in parts if part)
    return {
        "name": name,
        "transport": transport,
        "config": config,
        "args": args,
        "url": url,
        "endpoint": endpoint,
        "disabled": disabled,
    }


def commented_ids(path: str):
    """id закомментированных mcp-записей — чтобы показать «есть, но выключено вручную»."""
    found = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                match = re.match(r"\s*#\s*-\s*id:\s*([\w.\-]+)", line)
                if match:
                    found.append(match.group(1))
    except OSError:
        return []
    return found


# --------------------------------------------------------------------------
# MCP-хендшейк
# --------------------------------------------------------------------------

def _result(state, ms=None, tools=None, detail="", **extra):
    data = {"state": state, "ms": ms, "tools": tools, "detail": detail}
    data.update(extra)
    return data


def _why(exc) -> str:
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, ConnectionRefusedError):
        return "порт закрыт (connection refused)"
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return "таймаут"
    if isinstance(reason, ConnectionResetError):
        return "соединение сброшено"
    text = str(reason) or reason.__class__.__name__
    if "10061" in text:
        return "порт закрыт (connection refused)"
    if "10060" in text or "timed out" in text.lower():
        return "таймаут"
    return text


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _post(url, payload, session_id=None, timeout=6.0, _hop=0):
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json, text/event-stream")
    if session_id:
        request.add_header("Mcp-Session-Id", session_id)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", "replace")
            return response.status, text, response.headers.get("Mcp-Session-Id"), None
    except urllib.error.HTTPError as error:
        # некоторые серверы отвечают 307 на /mcp и ждут /mcp/ — идём по Location
        location = error.headers.get("Location") if error.headers else None
        if location and error.code in (301, 302, 303, 307, 308) and _hop < 3:
            return _post(
                urllib.parse.urljoin(url, location), payload, session_id, timeout, _hop + 1
            )
        text = ""
        try:
            text = error.read().decode("utf-8", "replace")
        except Exception:
            pass
        header = error.headers.get("Mcp-Session-Id") if error.headers else None
        return error.code, text, header, None
    except Exception as error:  # noqa: BLE001 — нужен любой сетевой сбой
        return 0, "", None, error


def _rpc_message(body: str):
    """Ответ может быть обычным JSON или SSE-потоком (data: {...})."""
    body = (body or "").strip()
    if not body:
        return None
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            return parsed
    except ValueError:
        pass
    message = None
    for block in re.split(r"\r?\n\r?\n", body):
        data = "\n".join(
            line[5:].strip() for line in block.splitlines() if line.strip().startswith("data:")
        )
        if not data:
            continue
        try:
            parsed = json.loads(data)
        except ValueError:
            continue
        if isinstance(parsed, dict) and ("result" in parsed or "error" in parsed):
            message = parsed
    return message


def probe_http(url: str, timeout: float):
    if not url:
        return _result("dead", detail="в конфиге нет url")
    started = time.perf_counter()

    def elapsed():
        return int((time.perf_counter() - started) * 1000)

    status_code, body, session_id, error = _post(
        url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
        },
        timeout=timeout,
    )
    if error is not None:
        return _result("dead", ms=elapsed(), detail=_why(error))

    message = _rpc_message(body)
    if message is None:
        snippet = " ".join((body or "").split())[:120]
        detail = f"HTTP {status_code}, ответ не похож на MCP"
        if snippet:
            detail += f": {snippet}"
        return _result("dead", ms=elapsed(), detail=detail)
    if "error" in message:
        err = message["error"]
        text = err.get("message") if isinstance(err, dict) else str(err)
        return _result("dead", ms=elapsed(), detail=f"initialize: {text}")

    server_info = (message.get("result") or {}).get("serverInfo") or {}
    server_name = server_info.get("name") or ""
    server_version = server_info.get("version") or ""
    label = f"{server_name} {server_version}".strip()

    # без этой нотификации часть серверов (например на FastMCP) отвечает на
    # tools/list ошибкой «Invalid request parameters»
    _post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session_id=session_id, timeout=timeout)

    status_code, body, _, error = _post(
        url,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        session_id=session_id,
        timeout=timeout,
    )
    if error is not None:
        return _result("warn", ms=elapsed(), detail=f"initialize ок, tools/list: {_why(error)}", server=label)

    message = _rpc_message(body)
    if message is None:
        return _result("warn", ms=elapsed(), detail="initialize ок, tools/list без ответа", server=label)
    if "error" in message:
        err = message["error"]
        text = err.get("message") if isinstance(err, dict) else str(err)
        return _result("warn", ms=elapsed(), detail=f"tools/list: {text}", server=label)

    tools = (message.get("result") or {}).get("tools") or []
    return _result("ok", ms=elapsed(), tools=len(tools), detail="", server=label)


class _LineReader(threading.Thread):
    def __init__(self, stream):
        super().__init__(daemon=True)
        self.stream = stream
        self.queue = Queue()

    def run(self):
        try:
            for line in self.stream:
                self.queue.put(line)
        except Exception:
            pass
        self.queue.put(None)


def _await_message(reader, wanted_ids, deadline):
    while time.monotonic() < deadline:
        timeout = max(0.05, deadline - time.monotonic())
        try:
            line = reader.queue.get(timeout=timeout)
        except Empty:
            return None
        if line is None:
            return None
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict) and message.get("id") in wanted_ids:
            return message
    return None


def _send(process, payload):
    try:
        process.stdin.write(json.dumps(payload) + "\n")
        process.stdin.flush()
    except (OSError, ValueError):
        pass


def probe_stdio(entry, timeout: float):
    config = entry["config"]
    command = config.get("command")
    args = entry["args"]
    if not command:
        return _result("dead", detail="в конфиге нет command")
    if not os.path.exists(command):
        return _result("dead", detail=f"файл не найден: {command}")

    started = time.perf_counter()

    def elapsed():
        return int((time.perf_counter() - started) * 1000)

    try:
        process = subprocess.Popen(
            [command] + args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as error:
        return _result("dead", ms=elapsed(), detail=f"не запускается: {error}")

    reader = _LineReader(process.stdout)
    reader.start()
    try:
        deadline = time.monotonic() + timeout
        _send(
            process,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL,
                    "capabilities": {},
                    "clientInfo": CLIENT_INFO,
                },
            },
        )
        message = _await_message(reader, {1}, deadline)
        if message is None:
            return _result("dead", ms=elapsed(), detail="процесс не ответил на initialize")
        if "error" in message:
            err = message["error"]
            text = err.get("message") if isinstance(err, dict) else str(err)
            return _result("dead", ms=elapsed(), detail=f"initialize: {text}")

        server_info = (message.get("result") or {}).get("serverInfo") or {}
        label = f"{server_info.get('name') or ''} {server_info.get('version') or ''}".strip()

        # stdio-серверы (особенно роутеры) на tools/list думают дольше, чем на
        # initialize, поэтому каждому этапу даём свой полный таймаут
        deadline = time.monotonic() + timeout
        _send(process, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        _send(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        message = _await_message(reader, {2}, deadline)
        if message is None:
            return _result("warn", ms=elapsed(), detail="initialize ок, tools/list без ответа", server=label)
        if "error" in message:
            err = message["error"]
            text = err.get("message") if isinstance(err, dict) else str(err)
            return _result("warn", ms=elapsed(), detail=f"tools/list: {text}", server=label)
        tools = (message.get("result") or {}).get("tools") or []
        return _result("ok", ms=elapsed(), tools=len(tools), detail="", server=label)
    finally:
        try:
            process.terminate()
            process.wait(timeout=3)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


def probe(entry, timeout: float, stdio_timeout: float = None):
    if entry["disabled"]:
        return _result("off", detail="disabled: true")
    if entry["transport"] in ("http", "streamable-http", "sse"):
        return probe_http(entry["url"], timeout)
    # stdio-серверы стартуют дольше: сначала поднимается процесс, потом он сам
    # обходит порты, поэтому им нужен отдельный, более щедрый таймаут
    return probe_stdio(entry, stdio_timeout or timeout)


# --------------------------------------------------------------------------
# инстансы EDT
# --------------------------------------------------------------------------

def _tcp_open(port: int, timeout: float = 0.25) -> bool:
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        sock.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def edt_projects(roots, port_range=None):
    """Каждый проект EDT пишет свой .mcp.json со списком серверов.

    Нам нужен только тот, что указывает на сам инстанс EDT: сначала ищем по
    имени ключа (1c-edt), иначе — по попаданию порта в диапазон роутера.
    Иначе первый же http-сервер (8002) будет выдан за «инстанс EDT».
    """
    low, high = None, None
    match = re.match(r"(\d+)\s*-\s*(\d+)$", (port_range or "").strip())
    if match:
        low, high = int(match.group(1)), int(match.group(2))

    rows = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            config_path = os.path.join(root, name, ".mcp.json")
            if not os.path.isfile(config_path):
                continue
            url = None
            try:
                with open(config_path, encoding="utf-8") as handle:
                    data = json.load(handle)
                candidates = []
                for key, value in (data.get("mcpServers") or {}).items():
                    candidate = value.get("url") if isinstance(value, dict) else None
                    if candidate and re.search(r"127\.0\.0\.1:(\d+)", candidate):
                        candidates.append((str(key), candidate))
                for key, candidate in candidates:
                    if "edt" in key.lower():
                        url = candidate
                        break
                if url is None and low is not None:
                    for _, candidate in candidates:
                        port = int(re.search(r"127\.0\.0\.1:(\d+)", candidate).group(1))
                        if low <= port <= high:
                            url = candidate
                            break
            except (OSError, ValueError):
                pass
            rows.append({"project": name, "url": url})
    return rows


def _format_width(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def render(servers, edt_rows, orphan_ports, config_path, commented) -> str:
    lines = []
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    lines.append(f"DSH MCP-статус — {stamp}")
    lines.append(f"конфиг: {config_path}")
    lines.append("")

    header = f"{'#':>2}  {'сервер':<16} {'транспорт':<10} {'эндпоинт':<44} {'статус':<11} {'мс':>6}  {'инстр.':>6}"
    lines.append(header)
    lines.append("-" * len(header))
    for index, item in enumerate(servers, 1):
        probe_result = item["probe"]
        label = STATE_LABEL.get(probe_result["state"], probe_result["state"])
        transport = TRANSPORT_LABEL.get(item["transport"], item["transport"])
        ms = probe_result["ms"]
        tools = probe_result["tools"]
        lines.append(
            f"{index:>2}  {_format_width(item['name'], 16):<16} "
            f"{_format_width(transport, 10):<10} "
            f"{_format_width(item['endpoint'], 44):<44} "
            f"{label:<11} "
            f"{(str(ms) if ms is not None else '-'):>6}  "
            f"{(str(tools) if tools is not None else '-'):>6}"
        )

    problems = [item for item in servers if item["probe"]["state"] == "dead"]
    partial = [item for item in servers if item["probe"]["state"] == "warn"]
    if problems or partial:
        lines.append("")
        for item in problems + partial:
            lines.append(f"  {item['name']}: {item['probe']['detail']}")

    if commented:
        lines.append("")
        lines.append("закомментировано в конфиге: " + ", ".join(commented))

    if edt_rows:
        lines.append("")
        lines.append(f"EDT-инстансы ({len(edt_rows)} проектов с .mcp.json)")
        sub = f"{'проект':<22} {'эндпоинт':<34} {'статус':<11} {'мс':>6}  {'инстр.':>6}"
        lines.append(sub)
        lines.append("-" * len(sub))
        for row in edt_rows:
            probe_result = row.get("probe") or {}
            label = STATE_LABEL.get(probe_result.get("state"), "—")
            ms = probe_result.get("ms")
            tools = probe_result.get("tools")
            lines.append(
                f"{_format_width(row['project'], 22):<22} "
                f"{_format_width(row['url'] or '—', 34):<34} "
                f"{label:<11} "
                f"{(str(ms) if ms is not None else '-'):>6}  "
                f"{(str(tools) if tools is not None else '-'):>6}"
            )
        dead_rows = [row for row in edt_rows if (row.get("probe") or {}).get("state") not in ("ok", None)]
        if dead_rows:
            lines.append("")
            for row in dead_rows:
                detail = (row.get("probe") or {}).get("detail") or ""
                state = (row.get("probe") or {}).get("state") or "dead"
                label = STATE_LABEL.get(state, state)
                lines.append(f"  {row['project']}: {label}{(' — ' + detail) if detail else ''}")
        if orphan_ports:
            lines.append("")
            lines.append("живые MCP-порты, не привязанные к проекту: " + ", ".join(map(str, orphan_ports)))

    total = len(servers)
    alive = sum(1 for item in servers if item["probe"]["state"] == "ok")
    off = sum(1 for item in servers if item["probe"]["state"] in ("off", "skipped"))
    dead = total - alive - off
    lines.append("")
    summary = f"итог: работают {alive} из {total - off}"
    if dead:
        summary += f", не отвечают {dead}"
    if off:
        summary += f", выключены {off}"
    if edt_rows:
        edt_alive = sum(1 for row in edt_rows if (row.get("probe") or {}).get("state") == "ok")
        summary += f"; EDT: {edt_alive} из {len(edt_rows)} инстансов на связи"
    lines.append(summary)
    return "\n".join(lines)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="Статус MCP-серверов, подключённых к DSH")
    parser.add_argument("--profile", default=None, help="каталог профиля DSH (по умолчанию ~/.dsh/profiles/desktop)")
    parser.add_argument("--timeout", type=float, default=6.0, help="таймаут HTTP-запроса, секунды")
    parser.add_argument("--stdio-timeout", type=float, default=30.0, help="таймаут stdio-сервера, секунды")
    parser.add_argument("--edt-timeout", type=float, default=2.5, help="таймаут одного инстанса EDT, секунды")
    parser.add_argument("--json", action="store_true", help="машинный вывод")
    parser.add_argument(
        "--out",
        default=None,
        help="писать JSON в файл вместо stdout (для вызывающих, которым нельзя читать пайп)",
    )
    parser.add_argument("--no-edt", action="store_true", help="не проверять инстансы EDT")
    parser.add_argument("--no-scan", action="store_true", help="не сканировать диапазон портов EDT")
    args = parser.parse_args()

    profile_dir = args.profile or default_profile_dir()
    config_path = os.path.join(profile_dir, "cordis.patch.yml")
    if not os.path.isfile(config_path):
        print(f"не найден конфиг профиля: {config_path}", file=sys.stderr)
        return 2

    with open(config_path, encoding="utf-8") as handle:
        tree = parse_yaml(handle.read())

    entries = []
    _collect_entries(tree, entries)
    servers = [describe_server(entry) for entry in entries]
    commented = commented_ids(config_path)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda item: probe(item, args.timeout, args.stdio_timeout), servers))
    for item, result in zip(servers, results):
        item["probe"] = result

    edt_rows = []
    orphan_ports = []
    if not args.no_edt:
        roots, port_range = [], "8760-8790"
        for item in servers:
            if item["name"] == "1c-edt" or "edt-mcp-router" in item["endpoint"]:
                roots_value = _arg_value(item["args"], "--roots")
                if roots_value:
                    roots.extend(part for part in roots_value.split(",") if part)
                ports_value = _arg_value(item["args"], "--ports")
                if ports_value:
                    port_range = ports_value
        if not roots:
            default_root = r"E:\EDT new"
            if os.path.isdir(default_root):
                roots.append(default_root)

        edt_rows = edt_projects(roots, port_range)
        known_ports = set()
        for row in edt_rows:
            match = re.search(r"127\.0\.0\.1:(\d+)", row["url"] or "")
            if match:
                known_ports.add(int(match.group(1)))

        with ThreadPoolExecutor(max_workers=8) as pool:
            probes = list(
                pool.map(
                    lambda row: probe_http(row["url"], args.edt_timeout)
                    if row["url"]
                    else _result("skipped", detail="нет url"),
                    edt_rows,
                )
            )
        for row, result in zip(edt_rows, probes):
            row["probe"] = result

        if not args.no_scan:
            match = re.match(r"(\d+)\s*-\s*(\d+)$", port_range.strip())
            if match:
                low, high = int(match.group(1)), int(match.group(2))
                ports = list(range(low, high + 1))
                with ThreadPoolExecutor(max_workers=16) as pool:
                    opened = [port for port, alive in zip(ports, pool.map(_tcp_open, ports)) if alive]
                candidates = [port for port in opened if port not in known_ports]
                if candidates:
                    urls = [f"http://127.0.0.1:{port}/mcp" for port in candidates]
                    with ThreadPoolExecutor(max_workers=8) as pool:
                        checks = list(pool.map(lambda url: probe_http(url, args.timeout), urls))
                    orphan_ports = [port for port, check in zip(candidates, checks) if check["state"] != "dead"]

    if args.json or args.out:
        payload = {
            "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "config": config_path,
            "servers": [
                {
                    "name": item["name"],
                    "transport": item["transport"],
                    "endpoint": item["endpoint"],
                    "state": item["probe"]["state"],
                    "ms": item["probe"]["ms"],
                    "tools": item["probe"]["tools"],
                    "detail": item["probe"]["detail"],
                }
                for item in servers
            ],
            "commentedOut": commented,
            "edt": [
                {
                    "project": row["project"],
                    "url": row["url"],
                    "state": (row.get("probe") or {}).get("state"),
                    "tools": (row.get("probe") or {}).get("tools"),
                    "detail": (row.get("probe") or {}).get("detail"),
                }
                for row in edt_rows
            ],
            "orphanPorts": orphan_ports,
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as handle:
                handle.write(text)
        else:
            print(text)
        return 0

    print(render(servers, edt_rows, orphan_ports, config_path, commented))
    dead = [item for item in servers if item["probe"]["state"] == "dead"]
    return 1 if dead else 0


if __name__ == "__main__":
    sys.exit(main())
