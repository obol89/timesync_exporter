#!/usr/bin/env python3
"""
timesync_exporter - Prometheus exporter for Linux time sync status.

Listens on :9108/metrics by default. Supports sfptpd, chrony, and ntpd.
"""

import argparse
import http.client
import logging
import os
import re
import shutil
import socket as _socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from typing import Dict, List, Optional, Tuple

EXPORTER_VERSION = "1.3.5"

DEFAULT_LISTEN = "0.0.0.0"
DEFAULT_PORT = 9108
DEFAULT_CACHE_SECONDS = 5.0
TIMEOUT_SYSTEMCTL = 1.5
TIMEOUT_SUBPROCESS = 2.0
TIMEOUT_CHRONY = 2.5
TIMEOUT_NTPD = 2.5
TIMEOUT_SFPTPD_SOCK = 5.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("timesync_exporter")

# Python 3.6 compatibility for ThreadingHTTPServer
try:
    from http.server import ThreadingHTTPServer
except ImportError:
    from http.server import HTTPServer

    class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    ThreadingHTTPServer = _ThreadingHTTPServer


# --- Utilities ---

def run_cmd(cmd, timeout=TIMEOUT_SUBPROCESS):
    # type: (List[str], float) -> Tuple[int, str, str]
    """Execute command. Returns (returncode, stdout, error_category)."""
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # Python 3.6 compatible (text=True is 3.7+)
            universal_newlines=True,
            timeout=timeout
        )
        return p.returncode, p.stdout, "none"
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception:
        return 125, "", "exec_error"


def systemctl_is_enabled(unit):
    # type: (str) -> bool
    rc, out, err = run_cmd(
        ["systemctl", "is-enabled", unit], TIMEOUT_SYSTEMCTL)
    if err != "none":
        return False
    return rc == 0 and out.strip() in ("enabled", "enabled-runtime")


def systemctl_is_active(unit):
    # type: (str) -> bool
    rc, out, err = run_cmd(["systemctl", "is-active", unit], TIMEOUT_SYSTEMCTL)
    return err == "none" and rc == 0 and out.strip() == "active"


def systemctl_unit_exists(unit):
    # type: (str) -> bool
    rc, out, err = run_cmd(
        ["systemctl", "show", "-p", "LoadState", unit], TIMEOUT_SYSTEMCTL)
    return err == "none" and rc == 0 and "LoadState=not-found" not in out


def detect_chrony_unit():
    # type: () -> str
    """Detect the correct chrony unit name.

    RHEL/CentOS use 'chronyd.service', Debian/Ubuntu use 'chrony.service'.
    One is often an alias for the other; is-enabled returns 'alias' for
    the non-canonical name, so check is-enabled first to find the real unit.
    """
    candidates = ["chronyd.service", "chrony.service"]
    # Prefer the canonical unit (is-enabled returns 'enabled', not 'alias')
    for unit in candidates:
        if systemctl_is_enabled(unit):
            return unit
    # Fall back to whichever is active
    for unit in candidates:
        if systemctl_is_active(unit):
            return unit
    for unit in candidates:
        if systemctl_unit_exists(unit):
            return unit
    return "chrony.service"


def detect_ntpd_unit():
    # type: () -> str
    """Detect the correct ntpd unit name.

    RHEL/CentOS use 'ntpd.service', Debian/Ubuntu use 'ntp.service'.
    """
    candidates = ["ntpd.service", "ntp.service"]
    for unit in candidates:
        if systemctl_is_enabled(unit):
            return unit
    for unit in candidates:
        if systemctl_is_active(unit):
            return unit
    for unit in candidates:
        if systemctl_unit_exists(unit):
            return unit
    return "ntpd.service"


def prom_escape(v):
    # type: (str) -> str
    return str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


# --- Metric formatting ---

class MetricWriter:
    """Handles Prometheus metric output with HELP/TYPE headers."""

    def __init__(self):
        self._emitted = set()  # type: set
        self._lines = []  # type: List[str]
        self._metadata = {}  # type: Dict[str, Tuple[str, str]]

    def declare(self, name, mtype, help_text):
        # type: (str, str, str) -> None
        self._metadata[name] = (mtype, help_text)

    def reset(self):
        # type: () -> None
        self._emitted.clear()
        self._lines = []

    def write(self, name, value, labels=None):
        # type: (str, float, Optional[Dict[str, str]]) -> None
        if name not in self._emitted and name in self._metadata:
            mtype, help_text = self._metadata[name]
            self._lines.append("# HELP {} {}\n# TYPE {} {}\n".format(
                name, help_text, name, mtype))
            self._emitted.add(name)

        if labels:
            lbl = ",".join('{}="{}"'.format(k, prom_escape(v))
                           for k, v in sorted(labels.items()))
            self._lines.append("{}{{{}}} {}\n".format(name, lbl, value))
        else:
            self._lines.append("{} {}\n".format(name, value))

    def write_one_hot(self, name, label, options, selected):
        # type: (str, str, List[str], str) -> None
        for opt in options:
            self.write(name, 1.0 if opt == selected else 0.0, {label: opt})

    def output(self):
        # type: () -> str
        return "".join(self._lines)


# --- sfptpd OpenMetrics parsing ---

_PROM_LINE_RE = re.compile(
    r'^(\w+)(\{[^}]*\})?\s+([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)')
_LABEL_RE = re.compile(r'(\w+)="([^"\\]*(?:\\.[^"\\]*)*)"')


def parse_prom_text(text):
    # type: (str) -> Dict[Tuple[str, Tuple], float]
    """Parse Prometheus text format into {(name, labels_tuple): value}."""
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _PROM_LINE_RE.match(line)
        if not m:
            continue
        name, labels_blob, val_str = m.groups()
        try:
            val = float(val_str)
        except ValueError:
            continue
        labels = tuple(sorted(_LABEL_RE.findall(labels_blob or "")))
        result[(name, labels)] = val
    return result


def get_metric(metrics, name, labels):
    # type: (Dict, str, Dict[str, str]) -> Optional[float]
    return metrics.get((name, tuple(sorted(labels.items()))))


class _UnixHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection over a Unix domain socket."""

    def __init__(self, sock_path, timeout):
        # type: (str, float) -> None
        super().__init__("localhost", timeout=timeout)
        self._sock_path = sock_path

    def connect(self):
        # type: () -> None
        self.sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._sock_path)


def _fetch_unix_http(sock_path, timeout):
    # type: (str, float) -> Tuple[Optional[str], str]
    """Fetch HTTP response body from a Unix socket using http.client."""
    try:
        conn = _UnixHTTPConnection(sock_path, timeout)
        conn.request("GET", "/metrics")
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        conn.close()
        return (body, "none") if body.strip() else (None, "empty_body")
    except _socket.timeout:
        return None, "timeout"
    except Exception:
        return None, "exec_error"


def sfptpd_metrics_via_sock(sock_paths, timeout=TIMEOUT_SFPTPD_SOCK):
    # type: (str, float) -> Tuple[Optional[str], str]
    """Fetch sfptpd OpenMetrics via Unix socket."""
    last_err = "sock_missing"
    seen = set()  # type: set
    for sock in (p.strip() for p in sock_paths.split(",") if p.strip()):
        if not os.path.exists(sock):
            continue
        real = os.path.realpath(sock)
        if real in seen:
            continue
        seen.add(real)
        body, err = _fetch_unix_http(sock, timeout)
        if err == "none":
            return body, "none"
        last_err = err
    return None, last_err


def choose_sfptpd_sync(metrics):
    # type: (Dict) -> Tuple[Optional[str], str]
    """Select best sfptpd sync instance from metrics."""
    # Try servo_info with clock=system first, but only if it is disciplining
    for (name, labels), val in metrics.items():
        if name == "servo_info" and val != 0:
            labels_dict = dict(labels)
            if labels_dict.get("clock") == "system" and labels_dict.get("sync"):
                sync = labels_dict["sync"]
                if get_metric(metrics, "is_disciplining", {"sync": sync}) == 1:
                    return sync, "servo_info"

    # Fall back to offset_snapshot_seconds
    sync_set = set()
    for (name, labels) in metrics:
        if name == "offset_snapshot_seconds":
            sync = dict(labels).get("sync")
            if sync:
                sync_set.add(sync)
    syncs = sorted(sync_set)
    if len(syncs) == 1:
        return syncs[0], "snapshot_single"
    if syncs:
        # Pick best: in_sync=1, lowest alarms, smallest offset
        def sort_key(s):
            return (
                -(get_metric(metrics, "in_sync", {"sync": s}) or 0),
                get_metric(metrics, "alarms", {"sync": s}) or 9999,
                abs(get_metric(
                    metrics, "offset_snapshot_seconds", {"sync": s}) or 1e9)
            )
        best = min(syncs, key=sort_key)
        return best, "snapshot_best"
    return None, "none"


# --- sfptpd topology parsing ---

_TOPO_STATE_RE = re.compile(r"^\s*state:\s*(\S+)", re.MULTILINE)
_OFFSET_RE = re.compile(r"^\s*([-+]?\d+(?:\.\d+)?)\s*(ns|us|µs|ms|s)\s*$")
_OFFSET_ITER_RE = re.compile(r"([-+]?\d+(?:\.\d+)?)\s*(ns|us|µs|ms|s)")

UNIT_TO_SEC = {"s": 1.0, "ms": 1e-3, "us": 1e-6, "µs": 1e-6, "ns": 1e-9}

_ACTIVE_SYNC_STATES = frozenset({"ptp-slave", "pps-slave", "ntp-slave"})


def _label_center(line, label):
    # type: (str, str) -> int
    """Return the center column position of *label* within *line*."""
    idx = line.find(label)
    if idx < 0:
        idx = 0
    return idx + len(label) // 2


def parse_sfptpd_topology(text):
    # type: (str) -> Tuple[Optional[str], Optional[float], Optional[float]]
    """Parse sfptpd topology file. Returns (state, system_offset, phc_offset)."""
    state_match = _TOPO_STATE_RE.search(text)
    state = state_match.group(1) if state_match else None

    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    system_offset = None  # type: Optional[float]
    phc_offset = None  # type: Optional[float]

    def find_offset_before(idx, label):
        # type: (int, str) -> Optional[float]
        target_col = _label_center(lines[idx], label)
        for j in range(idx - 1, max(-1, idx - 10), -1):
            raw = lines[j]
            matches = list(_OFFSET_ITER_RE.finditer(raw))
            if not matches:
                continue
            if len(matches) == 1:
                m = matches[0]
                return float(m.group(1)) * UNIT_TO_SEC.get(m.group(2), 1.0)
            # Multiple offsets on one line — pick closest by column position
            best = min(matches,
                       key=lambda m: abs((m.start() + m.end()) // 2 - target_col))
            return float(best.group(1)) * UNIT_TO_SEC.get(best.group(2), 1.0)
        return None

    _PHC_RE = re.compile(r"phc\d+\([^)]+\)")

    for i, line in enumerate(lines):
        # A line may contain multiple labels (two-column layout)
        if "system" in line and system_offset is None:
            system_offset = find_offset_before(i, "system")
        phc_match = _PHC_RE.search(line)
        if phc_match and phc_offset is None:
            phc_offset = find_offset_before(i, phc_match.group(0))

    return state, system_offset, phc_offset


# --- chrony / ntpd offsets ---

def chrony_offset_seconds():
    # type: () -> Optional[float]
    if shutil.which("chronyc") is None:
        return None
    rc, out, err = run_cmd(["chronyc", "tracking"], TIMEOUT_CHRONY)
    if err != "none" or rc != 0:
        return None
    m = re.search(
        r"Last offset\s*:\s*([-+]?\d+(?:\.\d+)?)\s*seconds", out, re.IGNORECASE)
    return float(m.group(1)) if m else None


def ntpd_offset_seconds():
    # type: () -> Optional[float]
    if shutil.which("ntpq") is None:
        return None
    rc, out, err = run_cmd(["ntpq", "-pn"], TIMEOUT_NTPD)
    if err != "none" or rc != 0:
        return None
    for line in out.splitlines():
        if line.strip().startswith("*"):
            parts = line.split()
            if len(parts) >= 9:
                try:
                    return float(parts[8]) * 1e-3  # ms to seconds
                except ValueError:
                    pass
    return None


def resolve_disciplining(sfptpd_disciplining, topo_state, topo_sys_off):
    # type: (Optional[float], Optional[str], Optional[float]) -> Tuple[Optional[float], str]
    """Resolve is_disciplining value and its source.

    Returns (value, source) where source is one of
    'openmetrics', 'topology_inferred', 'none'.
    """
    if sfptpd_disciplining == 1.0:
        return 1.0, "openmetrics"
    if topo_state in _ACTIVE_SYNC_STATES and topo_sys_off is not None:
        return 1.0, "topology_inferred"
    if sfptpd_disciplining is not None:
        return sfptpd_disciplining, "openmetrics"
    return None, "none"


# --- Exporter ---

class TimesyncExporter:
    def __init__(self, sfptpd_sock, sfptpd_topology, cache_seconds):
        # type: (str, str, float) -> None
        self.sfptpd_sock = sfptpd_sock
        self.sfptpd_topology = sfptpd_topology
        self.cache_seconds = cache_seconds
        self._cache_until = 0.0
        self._cache_body = ""
        self._collect_lock = threading.Lock()
        self._chrony_unit = detect_chrony_unit()
        self._ntpd_unit = detect_ntpd_unit()
        logger.info("Detected chrony unit: %s", self._chrony_unit)
        logger.info("Detected ntpd unit: %s", self._ntpd_unit)
        self.writer = MetricWriter()
        self._declare_metrics()

    def _declare_metrics(self):
        # type: () -> None
        w = self.writer
        w.declare("timesync_exporter_build_info",
                  "gauge", "Build information.")
        w.declare("timesync_exporter_last_scrape_timestamp_seconds",
                  "gauge", "Last scrape Unix timestamp.")
        w.declare("timesync_service_enabled", "gauge",
                  "Whether systemd unit is enabled.")
        w.declare("timesync_service_active", "gauge",
                  "Whether systemd unit is active.")
        w.declare("timesync_sfptpd_topology_ok", "gauge",
                  "Whether topology file was parsed.")
        w.declare("timesync_sfptpd_topology_mtime_seconds",
                  "gauge", "Topology file mtime.")
        w.declare("timesync_sfptpd_topology_age_seconds",
                  "gauge", "Seconds since topology modified.")
        w.declare("timesync_sfptpd_state", "gauge", "sfptpd state (one-hot).")
        w.declare("timesync_sfptpd_system_offset_seconds",
                  "gauge", "System clock offset from topology.")
        w.declare("timesync_sfptpd_system_offset_available",
                  "gauge", "Whether system offset available.")
        w.declare("timesync_sfptpd_phc_offset_seconds",
                  "gauge", "PHC offset from topology.")
        w.declare("timesync_sfptpd_phc_offset_available",
                  "gauge", "Whether PHC offset available.")
        w.declare("timesync_sfptpd_openmetrics_ok", "gauge",
                  "Whether OpenMetrics fetch succeeded.")
        w.declare("timesync_sfptpd_servo_info_series",
                  "gauge", "Count of servo_info samples.")
        w.declare("timesync_sfptpd_offset_snapshot_series",
                  "gauge", "Count of offset_snapshot samples.")
        w.declare("timesync_sfptpd_openmetrics_error", "gauge",
                  "OpenMetrics error category (one-hot).")
        w.declare("timesync_sfptpd_chosen_method", "gauge",
                  "Sync selection method (one-hot).")
        w.declare("timesync_sfptpd_in_sync", "gauge",
                  "sfptpd in_sync for chosen sync.")
        w.declare("timesync_sfptpd_alarms", "gauge",
                  "sfptpd alarms for chosen sync.")
        w.declare("timesync_sfptpd_is_disciplining", "gauge",
                  "sfptpd is_disciplining for chosen sync.")
        w.declare("timesync_sfptpd_is_disciplining_source", "gauge",
                  "Source of is_disciplining value (one-hot).")
        w.declare("timesync_sfptpd_system_offset_fallback_available",
                  "gauge", "Whether fallback offset available.")
        w.declare("timesync_sfptpd_system_offset_fallback_seconds",
                  "gauge", "Fallback system offset.")
        w.declare("timesync_exporter_scrape_success", "gauge",
                  "Whether component scrape succeeded.")
        w.declare("timesync_offset_available", "gauge",
                  "Whether offset for source is available.")
        w.declare("timesync_offset_seconds", "gauge",
                  "Offset reported by source.")
        w.declare("timesync_status", "gauge", "Overall status (one-hot).")

    def collect(self):
        # type: () -> str
        if time.monotonic() < self._cache_until:
            return self._cache_body

        with self._collect_lock:
            # Re-check cache inside lock (another thread may have refreshed it)
            if time.monotonic() < self._cache_until:
                return self._cache_body
            return self._collect_locked()

    def _collect_locked(self):
        # type: () -> str
        w = self.writer
        w.reset()
        t_wall = time.time()

        w.write("timesync_exporter_build_info",
                1.0, {"version": EXPORTER_VERSION})
        w.write("timesync_exporter_last_scrape_timestamp_seconds", t_wall)

        services = {
            "sfptpd": "sfptpd.service",
            "chrony": self._chrony_unit,
            "ntpd": self._ntpd_unit,
        }

        # Run ALL slow I/O in parallel (systemctl + sfptpd + chrony + ntpd)
        with ThreadPoolExecutor(max_workers=10) as pool:
            futs_enabled = {svc: pool.submit(systemctl_is_enabled, unit)
                            for svc, unit in services.items()}
            futs_active = {svc: pool.submit(systemctl_is_active, unit)
                           for svc, unit in services.items()}
            fut_sfptpd = pool.submit(
                sfptpd_metrics_via_sock, self.sfptpd_sock)
            fut_chrony = pool.submit(chrony_offset_seconds)
            fut_ntpd = pool.submit(ntpd_offset_seconds)

            enabled = {svc: f.result() for svc, f in futs_enabled.items()}
            active = {svc: f.result() for svc, f in futs_active.items()}
            sfptpd_text, fetch_err = fut_sfptpd.result()
            chrony_off_raw = fut_chrony.result()
            ntp_off_raw = fut_ntpd.result()

        for svc in ("sfptpd", "chrony", "ntpd"):
            w.write("timesync_service_enabled",
                    1.0 if enabled[svc] else 0.0, {"service": svc})
            w.write("timesync_service_active",
                    1.0 if active[svc] else 0.0, {"service": svc})

        # sfptpd topology (local file read, fast)
        topo_state, topo_sys_off, topo_phc_off = None, None, None
        topo_ok = 0.0

        if self.sfptpd_topology and os.path.exists(self.sfptpd_topology):
            try:
                stat = os.stat(self.sfptpd_topology)
                w.write("timesync_sfptpd_topology_mtime_seconds", stat.st_mtime)
                w.write("timesync_sfptpd_topology_age_seconds",
                        max(0.0, t_wall - stat.st_mtime))
                with open(self.sfptpd_topology, "r", encoding="utf-8", errors="replace") as f:
                    topo_state, topo_sys_off, topo_phc_off = parse_sfptpd_topology(
                        f.read())
                topo_ok = 1.0 if any(v is not None for v in (
                    topo_state, topo_sys_off, topo_phc_off)) else 0.0
            except Exception:
                pass

        w.write("timesync_sfptpd_topology_ok", topo_ok)

        states = ["ptp-slave", "ptp-master", "ptp-listening", "pps-slave",
                  "pps-master", "ntp-slave", "ntp-master", "freerun", "holdover", "other"]
        w.write_one_hot("timesync_sfptpd_state", "state", states,
                        topo_state if topo_state in states else "other")

        w.write("timesync_sfptpd_system_offset_available",
                1.0 if topo_sys_off is not None else 0.0)
        if topo_sys_off is not None:
            w.write("timesync_sfptpd_system_offset_seconds", topo_sys_off)

        w.write("timesync_sfptpd_phc_offset_available",
                1.0 if topo_phc_off is not None else 0.0)
        if topo_phc_off is not None:
            w.write("timesync_sfptpd_phc_offset_seconds", topo_phc_off)

        # sfptpd OpenMetrics
        sfptpd_ok = sfptpd_text is not None
        servo_count = offset_count = 0.0
        chosen_sync, chosen_method = None, "none"
        sfptpd_in_sync = sfptpd_alarms = sfptpd_disciplining = None
        fallback_offset, fallback_ok = None, 0.0

        if sfptpd_text:
            try:
                metrics = parse_prom_text(sfptpd_text)
                servo_count = sum(1 for (n, _) in metrics if n == "servo_info")
                offset_count = sum(
                    1 for (n, _) in metrics if n == "offset_snapshot_seconds")
                chosen_sync, chosen_method = choose_sfptpd_sync(metrics)

                if chosen_sync:
                    sfptpd_in_sync = get_metric(
                        metrics, "in_sync", {"sync": chosen_sync})
                    sfptpd_alarms = get_metric(
                        metrics, "alarms", {"sync": chosen_sync})
                    sfptpd_disciplining = get_metric(
                        metrics, "is_disciplining", {"sync": chosen_sync})

                if topo_sys_off is None:
                    for (name, labels), val in metrics.items():
                        if name == "servo_info" and val != 0 and dict(labels).get("clock") == "system":
                            sync = dict(labels).get("sync")
                            if sync:
                                fallback_offset = get_metric(
                                    metrics, "offset_snapshot_seconds", {"sync": sync})
                                fallback_ok = 1.0 if fallback_offset is not None else 0.0
                            break
            except Exception:
                sfptpd_ok = False
                fetch_err = "parse_error"

        w.write("timesync_sfptpd_openmetrics_ok", 1.0 if sfptpd_ok else 0.0)
        w.write("timesync_sfptpd_servo_info_series", servo_count)
        w.write("timesync_sfptpd_offset_snapshot_series", offset_count)

        errors = ["none", "sock_missing", "timeout",
                  "exec_error", "empty_body", "parse_error"]
        w.write_one_hot("timesync_sfptpd_openmetrics_error", "error",
                        errors, fetch_err if fetch_err in errors else "exec_error")
        w.write_one_hot("timesync_sfptpd_chosen_method", "method", [
                        "servo_info", "snapshot_single", "snapshot_best", "none"], chosen_method)

        if sfptpd_in_sync is not None:
            w.write("timesync_sfptpd_in_sync", sfptpd_in_sync)
        if sfptpd_alarms is not None:
            w.write("timesync_sfptpd_alarms", sfptpd_alarms)

        # is_disciplining: prefer OpenMetrics, fall back to topology inference
        disc_val, disc_source = resolve_disciplining(
            sfptpd_disciplining, topo_state, topo_sys_off)
        if disc_val is not None:
            w.write("timesync_sfptpd_is_disciplining", disc_val)
        w.write_one_hot("timesync_sfptpd_is_disciplining_source", "source",
                        ["openmetrics", "topology_inferred", "none"],
                        disc_source)

        w.write("timesync_sfptpd_system_offset_fallback_available", fallback_ok)
        if fallback_offset is not None:
            w.write("timesync_sfptpd_system_offset_fallback_seconds",
                    fallback_offset)

        # sfptpd offset (unified pattern with chrony/ntpd)
        sfptpd_sys = topo_sys_off if topo_sys_off is not None else fallback_offset
        w.write("timesync_offset_available",
                1.0 if sfptpd_sys is not None else 0.0, {"source": "sfptpd"})
        if sfptpd_sys is not None:
            w.write("timesync_offset_seconds",
                    sfptpd_sys, {"source": "sfptpd"})

        # Component scrape success
        w.write("timesync_exporter_scrape_success",
                1.0, {"component": "systemd"})
        w.write("timesync_exporter_scrape_success",
                1.0 if sfptpd_ok else 0.0, {"component": "sfptpd"})

        # chrony (only use result if chrony is active)
        chrony_off = chrony_off_raw if active["chrony"] else None
        w.write("timesync_exporter_scrape_success",
                1.0 if not active["chrony"] or chrony_off is not None else 0.0, {"component": "chrony"})
        w.write("timesync_offset_available",
                1.0 if chrony_off is not None else 0.0, {"source": "chrony"})
        if chrony_off is not None:
            w.write("timesync_offset_seconds",
                    chrony_off, {"source": "chrony"})

        # ntpd (only use result if ntpd is active)
        ntp_off = ntp_off_raw if active["ntpd"] else None
        w.write("timesync_exporter_scrape_success",
                1.0 if not active["ntpd"] or ntp_off is not None else 0.0, {"component": "ntpd"})
        w.write("timesync_offset_available",
                1.0 if ntp_off is not None else 0.0, {"source": "ntpd"})
        if ntp_off is not None:
            w.write("timesync_offset_seconds", ntp_off, {"source": "ntpd"})

        # Status
        any_offset = sfptpd_sys is not None or chrony_off is not None or ntp_off is not None
        status = "ok"
        if not any_offset:
            status = "no_source"
        elif enabled["sfptpd"] and not active["sfptpd"]:
            status = "sfptpd_enabled_not_active"
        elif enabled["sfptpd"] and active["sfptpd"] and not sfptpd_ok and sfptpd_sys is None:
            status = "sfptpd_metrics_unavailable"
        elif active["chrony"] and chrony_off is None:
            status = "chrony_metrics_unavailable"
        elif active["ntpd"] and ntp_off is None:
            status = "ntpd_metrics_unavailable"

        statuses = ["ok", "no_source", "sfptpd_enabled_not_active",
                    "sfptpd_metrics_unavailable", "chrony_metrics_unavailable", "ntpd_metrics_unavailable"]
        w.write_one_hot("timesync_status", "status", statuses, status)

        self._cache_body = w.output()
        self._cache_until = time.monotonic() + self.cache_seconds
        return self._cache_body


# --- HTTP Server ---

def make_handler(exporter):
    # type: (TimesyncExporter) -> type
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ("/metrics", "/"):
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"not found\n")
                return
            try:
                body = exporter.collect()
                self.send_response(200)
                self.send_header(
                    "Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            except Exception as e:
                logger.error("Error generating metrics: %s", e)
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b"Internal server error\n")

        def log_message(self, fmt, *args):
            logger.debug("%s - %s", self.client_address[0], fmt % args)

    return Handler


def main():
    ap = argparse.ArgumentParser(
        description="timesync_exporter v{} - Prometheus exporter for Linux time sync status".format(
            EXPORTER_VERSION)
    )
    ap.add_argument("--listen", default=DEFAULT_LISTEN,
                    help="Listen address (default: {})".format(DEFAULT_LISTEN))
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help="Listen port (default: {})".format(DEFAULT_PORT))
    ap.add_argument("--sfptpd-sock", default="/run/sfptpd/metrics.sock,/var/run/sfptpd/metrics.sock",
                    help="Comma-separated Unix socket paths for sfptpd OpenMetrics.")
    ap.add_argument("--sfptpd-topology", default="/var/lib/sfptpd/topology",
                    help="sfptpd topology file path.")
    ap.add_argument("--cache-seconds", type=float, default=DEFAULT_CACHE_SECONDS,
                    help="Metrics cache duration (default: {})".format(DEFAULT_CACHE_SECONDS))
    ap.add_argument("--verbose", action="store_true",
                    help="Enable debug logging")
    args = ap.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    logger.info("Starting timesync_exporter %s", EXPORTER_VERSION)

    exporter = TimesyncExporter(
        args.sfptpd_sock, args.sfptpd_topology, args.cache_seconds)
    httpd = ThreadingHTTPServer(
        (args.listen, args.port), make_handler(exporter))
    logger.info("Listening on %s:%d (/metrics)", args.listen, args.port)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
