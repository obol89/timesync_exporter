#!/usr/bin/env python3
"""Unit tests for parse_sfptpd_topology, choose_sfptpd_sync, and resolve_disciplining."""

import unittest

from timesync_exporter import (
    parse_sfptpd_topology, choose_sfptpd_sync, parse_prom_text,
    resolve_disciplining, UNIT_TO_SEC,
)


class TestParseSfptpdTopologySingleColumn(unittest.TestCase):
    """Layout 1: single offset per line."""

    TOPO = """\
state: ptp-slave

             -3.062 ns
                 |
                 v
              system

             -0.500 ns
                 |
                 v
           phc0(ens1f0)
"""

    def test_state(self):
        state, _, _ = parse_sfptpd_topology(self.TOPO)
        self.assertEqual(state, "ptp-slave")

    def test_system_offset(self):
        _, sys_off, _ = parse_sfptpd_topology(self.TOPO)
        self.assertAlmostEqual(sys_off, -3.062e-9)

    def test_phc_offset(self):
        _, _, phc_off = parse_sfptpd_topology(self.TOPO)
        self.assertAlmostEqual(phc_off, -0.500e-9)


class TestParseSfptpdTopologyTwoColumn(unittest.TestCase):
    """Layout 2: two offsets on the same line (PHC with multiple children)."""

    TOPO = """\
state: ptp-slave

             -0.688 ns                          -3.000 ns
                 v                                  v
              system                          phc3(sf2/sf3)
"""

    def test_state(self):
        state, _, _ = parse_sfptpd_topology(self.TOPO)
        self.assertEqual(state, "ptp-slave")

    def test_system_offset(self):
        _, sys_off, _ = parse_sfptpd_topology(self.TOPO)
        self.assertAlmostEqual(sys_off, -0.688e-9)

    def test_phc_offset(self):
        _, _, phc_off = parse_sfptpd_topology(self.TOPO)
        self.assertAlmostEqual(phc_off, -3.000e-9)


class TestParseSfptpdTopologyTwoColumnReversed(unittest.TestCase):
    """Layout 2 but with phc on the left, system on the right."""

    TOPO = """\
state: ptp-slave

             -5.123 ns                          -1.234 ns
                 v                                  v
           phc0(ens1f0)                           system
"""

    def test_system_offset(self):
        _, sys_off, _ = parse_sfptpd_topology(self.TOPO)
        self.assertAlmostEqual(sys_off, -1.234e-9)

    def test_phc_offset(self):
        _, _, phc_off = parse_sfptpd_topology(self.TOPO)
        self.assertAlmostEqual(phc_off, -5.123e-9)


class TestParseSfptpdTopologyMicroseconds(unittest.TestCase):
    """Verify unit conversion works for us/µs/ms."""

    TOPO = """\
state: ntp-slave

             +2.500 us
                 |
                 v
              system
"""

    def test_system_offset_us(self):
        _, sys_off, _ = parse_sfptpd_topology(self.TOPO)
        self.assertAlmostEqual(sys_off, 2.500e-6)


class TestParseSfptpdTopologyNoOffset(unittest.TestCase):
    """No offset lines at all — should return None."""

    TOPO = """\
state: freerun

              system
"""

    def test_no_offsets(self):
        state, sys_off, phc_off = parse_sfptpd_topology(self.TOPO)
        self.assertEqual(state, "freerun")
        self.assertIsNone(sys_off)
        self.assertIsNone(phc_off)


class TestParseSfptpdTopologyEmpty(unittest.TestCase):
    """Empty input."""

    def test_empty(self):
        state, sys_off, phc_off = parse_sfptpd_topology("")
        self.assertIsNone(state)
        self.assertIsNone(sys_off)
        self.assertIsNone(phc_off)


class TestChooseSfptpdSyncDirect(unittest.TestCase):
    """Single PTP instance: servo_info with is_disciplining=1 selects via servo_info."""

    METRICS_TEXT = """\
# HELP servo_info Servo info
# TYPE servo_info gauge
servo_info{sync="ptp1",clock="system",iface="ens1f0"} 1
# HELP is_disciplining Whether sync is disciplining
# TYPE is_disciplining gauge
is_disciplining{sync="ptp1"} 1
# HELP in_sync Whether sync is in sync
# TYPE in_sync gauge
in_sync{sync="ptp1"} 1
# HELP alarms Number of alarms
# TYPE alarms gauge
alarms{sync="ptp1"} 0
# HELP offset_snapshot_seconds Offset snapshot
# TYPE offset_snapshot_seconds gauge
offset_snapshot_seconds{sync="ptp1"} -3.062e-09
"""

    def test_selects_via_servo_info(self):
        metrics = parse_prom_text(self.METRICS_TEXT)
        sync, method = choose_sfptpd_sync(metrics)
        self.assertEqual(sync, "ptp1")
        self.assertEqual(method, "servo_info")


class TestChooseSfptpdSyncIntermediateServo(unittest.TestCase):
    """Intermediate servo: servo0 bridges system clock but is_disciplining=0.
    Should fall through to snapshot and select ptp_slave."""

    METRICS_TEXT = """\
# HELP servo_info Servo info
# TYPE servo_info gauge
servo_info{sync="servo0",clock="system",iface="ens1f0"} 1
servo_info{sync="ptp_slave",clock="phc0",iface="ens1f0"} 1
# HELP is_disciplining Whether sync is disciplining
# TYPE is_disciplining gauge
is_disciplining{sync="servo0"} 0
is_disciplining{sync="ptp_slave"} 1
# HELP in_sync Whether sync is in sync
# TYPE in_sync gauge
in_sync{sync="servo0"} 1
in_sync{sync="ptp_slave"} 1
# HELP alarms Number of alarms
# TYPE alarms gauge
alarms{sync="servo0"} 0
alarms{sync="ptp_slave"} 0
# HELP offset_snapshot_seconds Offset snapshot
# TYPE offset_snapshot_seconds gauge
offset_snapshot_seconds{sync="servo0"} 1.5e-06
offset_snapshot_seconds{sync="ptp_slave"} -3.062e-09
"""

    def test_falls_through_to_snapshot(self):
        metrics = parse_prom_text(self.METRICS_TEXT)
        sync, method = choose_sfptpd_sync(metrics)
        self.assertEqual(sync, "ptp_slave")
        self.assertIn(method, ("snapshot_single", "snapshot_best"))

    def test_does_not_select_servo0(self):
        metrics = parse_prom_text(self.METRICS_TEXT)
        sync, _ = choose_sfptpd_sync(metrics)
        self.assertNotEqual(sync, "servo0")


class TestResolveDiscipliningOpenMetrics(unittest.TestCase):
    """OpenMetrics reports is_disciplining=1 — use it directly."""

    def test_value_and_source(self):
        val, src = resolve_disciplining(1.0, "ptp-slave", -3e-9)
        self.assertEqual(val, 1.0)
        self.assertEqual(src, "openmetrics")

    def test_openmetrics_wins_even_without_topology(self):
        val, src = resolve_disciplining(1.0, None, None)
        self.assertEqual(val, 1.0)
        self.assertEqual(src, "openmetrics")


class TestResolveDiscipliningTopologyFallback(unittest.TestCase):
    """OpenMetrics missed is_disciplining (0 or None), topology infers it."""

    def test_fallback_from_zero(self):
        val, src = resolve_disciplining(0.0, "ptp-slave", -3e-9)
        self.assertEqual(val, 1.0)
        self.assertEqual(src, "topology_inferred")

    def test_fallback_from_none(self):
        val, src = resolve_disciplining(None, "ntp-slave", -1e-6)
        self.assertEqual(val, 1.0)
        self.assertEqual(src, "topology_inferred")

    def test_pps_slave(self):
        val, src = resolve_disciplining(0.0, "pps-slave", -5e-9)
        self.assertEqual(val, 1.0)
        self.assertEqual(src, "topology_inferred")

    def test_no_fallback_for_freerun(self):
        """freerun is not an active sync state — no inference."""
        val, src = resolve_disciplining(0.0, "freerun", -3e-9)
        self.assertEqual(val, 0.0)
        self.assertEqual(src, "openmetrics")

    def test_no_fallback_without_sys_offset(self):
        """Active state but no system offset — no inference."""
        val, src = resolve_disciplining(0.0, "ptp-slave", None)
        self.assertEqual(val, 0.0)
        self.assertEqual(src, "openmetrics")


class TestResolveDiscipliningNone(unittest.TestCase):
    """Neither OpenMetrics nor topology provide information."""

    def test_all_none(self):
        val, src = resolve_disciplining(None, None, None)
        self.assertIsNone(val)
        self.assertEqual(src, "none")

    def test_inactive_state_no_openmetrics(self):
        val, src = resolve_disciplining(None, "freerun", -3e-9)
        self.assertIsNone(val)
        self.assertEqual(src, "none")


if __name__ == "__main__":
    unittest.main()
