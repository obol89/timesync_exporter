# timesync_exporter

Prometheus exporter for Linux time sync status (sfptpd, chrony, ntpd).
Exposes offset and service status for each source independently — no
source selection or preference is applied by the exporter.

## Setup

```bash
systemctl daemon-reload
systemctl enable --now timesync_exporter
```

Test:

```bash
curl -s http://127.0.0.1:9108/metrics
```

## Flags

```bash
timesync_exporter.py --help
```

| Flag                 | Default                                                 | Description                              |
| -------------------- | ------------------------------------------------------- | ---------------------------------------- |
| `--listen`           | `0.0.0.0`                                               | Listen address                           |
| `--port`             | `9108`                                                  | Listen port                              |
| `--sfptpd-sock`      | `/run/sfptpd/metrics.sock,/var/run/sfptpd/metrics.sock` | Comma-separated sfptpd Unix socket paths |
| `--sfptpd-topology`  | `/var/lib/sfptpd/topology`                              | sfptpd topology file path                |
| `--cache-seconds`    | `5`                                                     | Metrics cache duration in seconds        |
| `--verbose`          | off                                                     | Enable debug logging                     |

## Key metrics

All three sources report offsets via the same uniform interface:

```
timesync_offset_available{source="sfptpd|chrony|ntpd"}  0 or 1
timesync_offset_seconds{source="sfptpd|chrony|ntpd"}    offset in seconds
```

Service status:

```
timesync_service_enabled{service="sfptpd|chrony|ntpd"}  0 or 1
timesync_service_active{service="sfptpd|chrony|ntpd"}   0 or 1
```

Overall status (one-hot):

```
timesync_status{status="ok|no_source|sfptpd_enabled_not_active|sfptpd_metrics_unavailable|chrony_metrics_unavailable|ntpd_metrics_unavailable"}
```

sfptpd-specific (topology + OpenMetrics):

```
timesync_sfptpd_state{state="ptp-slave|pps-slave|..."}  one-hot
timesync_sfptpd_system_offset_seconds                   from topology
timesync_sfptpd_phc_offset_seconds                      from topology
timesync_sfptpd_in_sync                                 from OpenMetrics
timesync_sfptpd_alarms                                  from OpenMetrics
timesync_sfptpd_is_disciplining                         from OpenMetrics
```

## Unit normalization

All offsets are exported in **seconds**, following Prometheus naming conventions.
Each source reports in its native unit and the exporter converts:

| Source             | Native unit  | Conversion                |
| ------------------ | ------------ | ------------------------- |
| sfptpd topology    | ns/us/ms/s   | multiplied by unit factor |
| sfptpd OpenMetrics | seconds      | none (already base unit)  |
| chrony (`chronyc`) | seconds      | none                      |
| ntpd (`ntpq`)      | milliseconds | `* 1e-3`                  |

## Prometheus scrape config

```yaml
scrape_configs:
  - job_name: timesync
    static_configs:
      - targets: ["<hostname>:9108"]
```
