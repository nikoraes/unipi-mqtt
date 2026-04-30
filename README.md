# unipi-mqtt

Bridges a [UniPi](https://www.unipi.technology/) Neuron / Axon device running [Evok](https://github.com/UniPiTechnology/evok) to **Home Assistant** via **MQTT Discovery**.

Entities (lights, switches, binary sensors) appear automatically in Home Assistant — no manual YAML configuration required.
Local direct-wiring (switch → relay) works immediately and independently of the network or Home Assistant.

## Features

- **MQTT Discovery** — outputs and inputs register themselves in HA automatically
- **Press types** — pushbuttons distinguish `short_press`, `long_press`, and `double_press`; each can trigger a different action on a different output
- **Auto-off timers** — any link can set `auto_off_s` to automatically turn an output off after N seconds
- **Motion sensors** — auto-publish OFF after a configurable delay
- **Fully async** — single-process, no threads, low overhead
- **Docker-ready** — run on the UniPi itself or on a central server / Kubernetes cluster

## Requirements

- Python 3.11+ **or** Docker
- A UniPi device with [Evok](https://evok.api-docs.io/) running (WebSocket + REST API)
- An MQTT broker (e.g. Mosquitto, the one bundled with Home Assistant)

## Quick start (Python)

```bash
pip install -r requirements.txt
# Edit config.yaml with your IPs and devices
python unipi_mqtt.py
```

## Quick start (Docker)

```bash
# Edit config.yaml first, then:
docker compose up -d
```

To run on a central NUC / Kubernetes cluster, point `unipi.ws_url` and `unipi.api_url` in `config.yaml` at the UniPi's IP address and mount `config.yaml` as a volume or ConfigMap.

## Configuration

All configuration lives in `config.yaml`. See the inline comments there for the full reference.

### `mqtt` section

```yaml
mqtt:
  host: "192.168.1.10"
  port: 1883
  username: "myuser"
  password: "mypass"
  client_id: "unipi-mqtt"
  discovery_prefix: "homeassistant"
```

### `unipi` section

```yaml
unipi:
  ws_url: "ws://192.168.1.100/ws"
  api_url: "http://192.168.1.100:8080/rest"
```

### Outputs

Each output maps a UniPi relay/digital output to an HA entity.

```yaml
outputs:
  - id: "relay_2_02" # unique id used in input links
    circuit: "2_02" # UniPi circuit identifier
    dev: "relay" # UniPi device type: relay or output
    name: "Badkamer Meubel"
    ha_component: "light" # HA entity type: light or switch
```

### Inputs

Each input maps a UniPi digital input to an HA binary sensor and optionally links it to one or more outputs.

| Field                    | Description                                                             |
| ------------------------ | ----------------------------------------------------------------------- |
| `circuit`                | UniPi circuit identifier                                                |
| `name`                   | Human-readable name, used in HA entity names and unique IDs             |
| `type`                   | `pushbutton`, `switch`, `contact`, or `motion`                          |
| `normal`                 | `no` (normally-open) or `nc` (normally-closed)                          |
| `delay_s`                | Auto-off delay in seconds (motion sensors only)                         |
| `ha_publish`             | `true` (default) to expose as a binary sensor in HA                     |
| `short_press_max_ms`     | Max duration for a short press — default `1000` ms                      |
| `long_press_max_ms`      | Max duration for a long press — default `4000` ms                       |
| `double_press_window_ms` | Max gap between two short presses to count as double — default `400` ms |

### Links (input → output)

Each input can have multiple `links`. A link fires only when its `trigger` matches the event on the input.

| Trigger        | Input type       | Description                                    |
| -------------- | ---------------- | ---------------------------------------------- |
| `short_press`  | pushbutton       | Quick tap (< `short_press_max_ms`)             |
| `long_press`   | pushbutton       | Held press (≥ `short_press_max_ms`)            |
| `double_press` | pushbutton       | Two quick taps within `double_press_window_ms` |
| `change`       | switch / contact | Any state change                               |
| `on`           | motion           | Motion detected                                |

Link fields:

| Field        | Description                                                       |
| ------------ | ----------------------------------------------------------------- |
| `output`     | `id` of the output to control                                     |
| `action`     | `toggle` (default), `on`, or `off`                                |
| `trigger`    | See table above                                                   |
| `auto_off_s` | Seconds after which the output turns off automatically (optional) |

### Example — three press types on one button

```yaml
inputs:
  - circuit: "2_09"
    name: "Drukknop Eetkamer A"
    type: "pushbutton"
    links:
      # Short press: toggle the light
      - output: "relay_2_05"
        action: "toggle"
        trigger: "short_press"
      # Long press: turn off unconditionally
      - output: "relay_2_05"
        action: "off"
        trigger: "long_press"
      # Double press: turn on with a 30-minute auto-off
      - output: "relay_2_05"
        action: "on"
        trigger: "double_press"
        auto_off_s: 1800
```

### Example — motion sensor with auto-off

```yaml
inputs:
  - circuit: "1_01"
    name: "Badkamer Bewegingsmelder"
    type: "motion"
    normal: "no"
    delay_s: 120
    ha_publish: true
    links:
      - output: "relay_2_02"
        action: "on"
        trigger: "on"
```

### Example — wall switch controlling a light

```yaml
inputs:
  - circuit: "2_01"
    name: "Schakelaar Gang"
    type: "switch"
    normal: "no"
    ha_publish: true
    links:
      - output: "relay_2_04"
        action: "toggle"
        trigger: "change"
```

## Running as a systemd service (on the UniPi)

```ini
# /etc/systemd/system/unipi-mqtt.service
[Unit]
Description=UniPi MQTT bridge
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/unipi-mqtt/unipi_mqtt.py
WorkingDirectory=/opt/unipi-mqtt
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now unipi-mqtt
```

## Home Assistant integration

No manual HA configuration is needed. Once the bridge is running, all configured outputs and inputs appear automatically under **Settings → Devices & Services → MQTT**.

- Outputs are registered as `light` or `switch` entities
- Inputs are registered as `binary_sensor` entities with the appropriate `device_class`
- All entities include an availability topic so HA shows them as unavailable if the bridge stops

## Architecture

```
UniPi Evok WebSocket  ──►  unipi_mqtt.py  ──►  MQTT broker  ──►  Home Assistant
Home Assistant MQTT command  ──►  unipi_mqtt.py  ──►  Evok REST API
```

The bridge runs a single async event loop with two concurrent tasks:

1. **Evok WS listener** — receives real-time input/output state changes from Evok
2. **MQTT command listener** — receives `ON`/`OFF` commands from Home Assistant

Local links (e.g. a pushbutton directly toggling a relay) are executed within the same event loop and work without MQTT or Home Assistant.
