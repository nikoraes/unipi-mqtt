#!/usr/bin/env python3
"""
unipi_mqtt – Bridges a UniPi / Evok device to Home Assistant via MQTT Discovery.

Architecture
────────────
  UniPi Evok WebSocket  ──►  this script  ──►  MQTT broker  ──►  Home Assistant
  Home Assistant MQTT command  ──►  this script  ──►  Evok REST API

Local direct links (switch → relay toggle) also work without HA/MQTT.
Pushbutton press types (short, long, double) are detected locally and can
trigger different output actions, with optional auto-off timers.

Configuration: config.yaml  (see example for full field documentation)
"""

import asyncio
import json
import logging
import re
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, cast

import aiohttp
import aiomqtt
import yaml

# ──────────────────────────────────────────────────────────────────────────────
# Config loading
# ──────────────────────────────────────────────────────────────────────────────


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def slugify(name: str) -> str:
    """Convert a name to a safe lowercase slug for use in MQTT topics / unique ids."""
    return re.sub(r"[^a-z0-9_]", "_", name.lower()).strip("_")


# ──────────────────────────────────────────────────────────────────────────────
# State
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class OutputState:
    id: str
    circuit: str
    dev: str
    name: str
    ha_component: str = "switch"
    state: bool = False  # current known state


@dataclass
class InputState:
    circuit: str
    name: str
    normal: str  # "no" or "nc"
    type: str  # "switch", "pushbutton", "contact", "motion"
    delay_s: int = 0
    ha_publish: bool = True
    links: list = field(default_factory=list)
    # Pushbutton press-duration thresholds (milliseconds)
    short_press_max_ms: int = 1000
    long_press_max_ms: int = 4000
    double_press_window_ms: int = 400
    # Internal runtime state (not set from config)
    _last_raw: int = field(default=-1, init=False, repr=False)
    _press_start: float = field(default=0.0, init=False, repr=False)
    _last_release: float = field(default=0.0, init=False, repr=False)
    _pending_short_task: Optional[asyncio.Task] = field(
        default=None, init=False, repr=False
    )


# ──────────────────────────────────────────────────────────────────────────────
# MQTT topic helpers
# ──────────────────────────────────────────────────────────────────────────────


class Topics:
    def __init__(self, discovery_prefix: str, device_name: str):
        self.dp = discovery_prefix
        self.dn = slugify(device_name)

    def output_state(self, out: OutputState) -> str:
        return f"unipi/{self.dn}/{slugify(out.name)}/state"

    def output_command(self, out: OutputState) -> str:
        return f"unipi/{self.dn}/{slugify(out.name)}/set"

    def input_state(self, inp: InputState) -> str:
        return f"unipi/{self.dn}/{slugify(inp.name)}/state"

    def availability(self, device_name: str) -> str:
        return f"unipi/{slugify(device_name)}/available"

    def discovery_output(self, out: OutputState) -> str:
        slug = slugify(out.name)
        return f"{self.dp}/{out.ha_component}/{self.dn}_{slug}/config"

    def discovery_input(self, inp: InputState) -> str:
        slug = slugify(inp.name)
        return f"{self.dp}/binary_sensor/{self.dn}_{slug}/config"


# ──────────────────────────────────────────────────────────────────────────────
# Evok REST helper
# ──────────────────────────────────────────────────────────────────────────────


class EvokRest:
    def __init__(self, api_url: str, session: aiohttp.ClientSession):
        self._base = api_url.rstrip("/")
        self._session = session

    async def set_output(self, dev: str, circuit: str, value: int) -> bool:
        url = f"{self._base}/{dev}/{circuit}"
        try:
            async with self._session.post(
                url, data={"value": value}, timeout=aiohttp.ClientTimeout(total=5)
            ) as r:
                if r.status == 200:
                    return True
                logging.error(
                    "Evok REST error %s for %s/%s value=%s",
                    r.status,
                    dev,
                    circuit,
                    value,
                )
                return False
        except Exception as exc:
            logging.error("Evok REST exception for %s/%s: %s", dev, circuit, exc)
            return False

    async def get_output(self, dev: str, circuit: str) -> Optional[dict]:
        url = f"{self._base}/{dev}/{circuit}"
        try:
            async with self._session.get(
                url, timeout=aiohttp.ClientTimeout(total=5)
            ) as r:
                if r.status == 200:
                    return await r.json()
                return None
        except Exception as exc:
            logging.error("Evok REST GET exception for %s/%s: %s", dev, circuit, exc)
            return None


# ──────────────────────────────────────────────────────────────────────────────
# Main bridge
# ──────────────────────────────────────────────────────────────────────────────


class UnipiBridge:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.device_name = cfg.get("device_name", "unipi")
        self.topics = Topics(
            discovery_prefix=cfg["mqtt"].get("discovery_prefix", "homeassistant"),
            device_name=self.device_name,
        )
        self.availability_topic = self.topics.availability(self.device_name)

        # Build state dicts keyed by circuit
        self.outputs: dict[str, OutputState] = {}
        for o in cfg.get("outputs", []):
            state = OutputState(
                id=o["id"],
                circuit=o["circuit"],
                dev=o["dev"],
                name=o["name"],
                ha_component=o.get("ha_component", "switch"),
            )
            self.outputs[o["id"]] = state

        self.inputs: dict[str, InputState] = {}
        for i in cfg.get("inputs", []):
            state = InputState(
                circuit=i["circuit"],
                name=i["name"],
                normal=i.get("normal", "no"),
                type=i.get("type", "pushbutton"),
                delay_s=i.get("delay_s", 0),
                ha_publish=i.get("ha_publish", True),
                links=i.get("links", []),
                short_press_max_ms=i.get("short_press_max_ms", 1000),
                long_press_max_ms=i.get("long_press_max_ms", 4000),
                double_press_window_ms=i.get("double_press_window_ms", 400),
            )
            self.inputs[i["circuit"]] = state

        self._mqtt: aiomqtt.Client = cast(aiomqtt.Client, None)
        self._evok: EvokRest = cast(EvokRest, None)
        self._motion_tasks: dict[str, asyncio.Task] = {}
        self._auto_off_tasks: dict[str, asyncio.Task] = {}

    # ── HA discovery ──────────────────────────────────────────────────────────

    def _device_payload(self) -> dict:
        return {
            "identifiers": [slugify(self.device_name)],
            "name": self.device_name,
            "model": "UniPi Neuron",
            "manufacturer": "UniPi Technology",
        }

    async def _publish_discovery(self):
        for out in self.outputs.values():
            slug = slugify(out.name)
            uid = f"{slugify(self.device_name)}_{slug}"
            payload = {
                "name": out.name,
                "unique_id": uid,
                "state_topic": self.topics.output_state(out),
                "command_topic": self.topics.output_command(out),
                "payload_on": "ON",
                "payload_off": "OFF",
                "availability_topic": self.availability_topic,
                "retain": True,
                "device": self._device_payload(),
            }
            topic = self.topics.discovery_output(out)
            await self._mqtt.publish(topic, json.dumps(payload), retain=True, qos=1)
            logging.info("Published discovery for output: %s", out.name)

        for inp in self.inputs.values():
            if not inp.ha_publish:
                continue
            slug = slugify(inp.name)
            uid = f"{slugify(self.device_name)}_{slug}"
            device_class = {
                "motion": "motion",
                "contact": "door",
                "pushbutton": "occupancy",
                "switch": None,
            }.get(inp.type)
            payload = {
                "name": inp.name,
                "unique_id": uid,
                "state_topic": self.topics.input_state(inp),
                "payload_on": "ON",
                "payload_off": "OFF",
                "availability_topic": self.availability_topic,
                "retain": False,
                "device": self._device_payload(),
            }
            if device_class:
                payload["device_class"] = device_class
            topic = self.topics.discovery_input(inp)
            await self._mqtt.publish(topic, json.dumps(payload), retain=True, qos=1)
            logging.info("Published discovery for input: %s", inp.name)

    # ── Output control ────────────────────────────────────────────────────────

    async def _set_output(self, out: OutputState, on: bool):
        ok = await self._evok.set_output(out.dev, out.circuit, 1 if on else 0)
        if ok:
            out.state = on
            state_str = "ON" if on else "OFF"
            await self._mqtt.publish(
                self.topics.output_state(out), state_str, retain=True, qos=1
            )
            logging.info("Output %s → %s", out.name, state_str)

    async def _toggle_output(self, out: OutputState):
        await self._set_output(out, not out.state)

    def _schedule_auto_off(self, out: OutputState, delay_s: int):
        """Cancel any existing auto-off timer for this output and start a new one."""
        existing = self._auto_off_tasks.get(out.id)
        if existing and not existing.done():
            existing.cancel()
        self._auto_off_tasks[out.id] = asyncio.create_task(
            self._auto_off_output(out, delay_s)
        )

    async def _auto_off_output(self, out: OutputState, delay_s: int):
        await asyncio.sleep(delay_s)
        logging.info("Auto-off triggered for %s after %ss", out.name, delay_s)
        await self._set_output(out, False)

    # ── Input handling ────────────────────────────────────────────────────────

    async def _fire_input_links(self, inp: InputState, press_type: str):
        """
        Execute all links whose `trigger` matches press_type.

        For pushbuttons, trigger is one of: short_press, long_press, double_press.
        For switch/contact/motion the trigger field is unused (all links fire).
        """
        for link in inp.links:
            trigger = link.get("trigger", "short_press")
            if trigger != press_type:
                continue
            out = self.outputs.get(link["output"])
            if out is None:
                logging.warning(
                    "Input %s links to unknown output id '%s'",
                    inp.circuit,
                    link["output"],
                )
                continue
            action = link.get("action", "toggle")
            if action == "toggle":
                await self._toggle_output(out)
            elif action == "on":
                await self._set_output(out, True)
            elif action == "off":
                await self._set_output(out, False)
            # Schedule auto-off only when the output ended up ON
            auto_off_s = link.get("auto_off_s")
            if auto_off_s and out.state:
                self._schedule_auto_off(out, int(auto_off_s))

    async def _delayed_short_press(self, inp: InputState):
        """Wait for the double-press window, then fire short_press links."""
        await asyncio.sleep(inp.double_press_window_ms / 1000)
        inp._pending_short_task = None
        await self._fire_input_links(inp, "short_press")

    async def _handle_input_change(self, circuit: str, value: int):
        inp = self.inputs.get(circuit)
        if inp is None:
            return

        now = asyncio.get_event_loop().time()

        # Always publish the raw sensor state to MQTT
        if inp.ha_publish:
            logical_on = (value == 1) if inp.normal == "no" else (value == 0)
            await self._mqtt.publish(
                self.topics.input_state(inp),
                "ON" if logical_on else "OFF",
                retain=False,
                qos=0,
            )

        if inp.type == "pushbutton":
            if value == 1:  # press down
                inp._press_start = now
                # If a deferred short-press is pending (waiting for possible
                # double press) cancel it; we'll decide once this press releases.
                if inp._pending_short_task and not inp._pending_short_task.done():
                    inp._pending_short_task.cancel()
                    inp._pending_short_task = None

            elif value == 0 and inp._press_start > 0:  # release
                duration_ms = (now - inp._press_start) * 1000
                inp._press_start = 0.0

                if inp.short_press_max_ms <= duration_ms < inp.long_press_max_ms:
                    # Long press – fire immediately, reset double-press tracking
                    inp._last_release = 0.0
                    await self._fire_input_links(inp, "long_press")

                elif duration_ms < inp.short_press_max_ms:
                    since_last_ms = (now - inp._last_release) * 1000
                    if (
                        inp._last_release > 0
                        and since_last_ms < inp.double_press_window_ms
                    ):
                        # Second quick release within window → double press
                        inp._last_release = 0.0
                        await self._fire_input_links(inp, "double_press")
                    else:
                        # First short press – wait briefly before firing so a
                        # following press can upgrade it to a double press.
                        inp._last_release = now
                        inp._pending_short_task = asyncio.create_task(
                            self._delayed_short_press(inp)
                        )
                # else: duration >= long_press_max_ms – too long, ignore

        elif inp.type in ("switch", "contact"):
            if value != inp._last_raw:
                inp._last_raw = value
                await self._fire_input_links(inp, "change")

        elif inp.type == "motion":
            inp._last_raw = value
            if value == 1:
                await self._fire_input_links(inp, "on")
                if inp.delay_s > 0:
                    existing = self._motion_tasks.get(circuit)
                    if existing and not existing.done():
                        existing.cancel()
                    self._motion_tasks[circuit] = asyncio.create_task(
                        self._motion_auto_off(inp)
                    )

    async def _motion_auto_off(self, inp: InputState):
        await asyncio.sleep(inp.delay_s)
        if inp.ha_publish:
            await self._mqtt.publish(
                self.topics.input_state(inp), "OFF", retain=False, qos=0
            )
        logging.debug("Motion auto-off for %s", inp.name)

    # ── Evok WebSocket listener ───────────────────────────────────────────────

    async def _evok_ws_loop(self):
        ws_url = self.cfg["unipi"]["ws_url"]
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(ws_url, heartbeat=10) as ws:
                        logging.info("Connected to Evok WebSocket %s", ws_url)
                        await self._sync_initial_output_states()
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                await self._handle_evok_message(msg.data)
                            elif msg.type in (
                                aiohttp.WSMsgType.ERROR,
                                aiohttp.WSMsgType.CLOSE,
                            ):
                                logging.warning(
                                    "Evok WebSocket closed/error, reconnecting…"
                                )
                                break
            except Exception as exc:
                logging.error("Evok WebSocket error: %s – reconnecting in 5 s", exc)
            await asyncio.sleep(5)

    async def _handle_evok_message(self, raw: str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        # Evok sends either a dict or a list of dicts
        items = [data] if isinstance(data, dict) else data

        for item in items:
            if not isinstance(item, dict):
                continue
            dev = item.get("dev")
            circuit = item.get("circuit")
            value = item.get("value")

            if dev == "input" and circuit is not None and value is not None:
                await self._handle_input_change(str(circuit), int(value))

            elif (
                dev in ("relay", "output") and circuit is not None and value is not None
            ):
                # Keep our output state in sync with what Evok reports
                # (covers changes made outside this script)
                for out in self.outputs.values():
                    if out.circuit == str(circuit) and out.dev == dev:
                        out.state = bool(value)
                        state_str = "ON" if out.state else "OFF"
                        await self._mqtt.publish(
                            self.topics.output_state(out), state_str, retain=True, qos=1
                        )

    async def _sync_initial_output_states(self):
        """Read current output states from Evok on startup."""
        for out in self.outputs.values():
            data = await self._evok.get_output(out.dev, out.circuit)
            if data and "value" in data:
                out.state = bool(data["value"])
                state_str = "ON" if out.state else "OFF"
                await self._mqtt.publish(
                    self.topics.output_state(out), state_str, retain=True, qos=1
                )
        logging.info("Initial output states synced from Evok")

    # ── MQTT command listener ─────────────────────────────────────────────────

    async def _mqtt_command_loop(self):
        command_topics = {
            self.topics.output_command(out): out for out in self.outputs.values()
        }
        for topic in command_topics:
            await self._mqtt.subscribe(topic, qos=1)
            logging.debug("Subscribed to MQTT command topic: %s", topic)

        async for message in self._mqtt.messages:
            topic_str = str(message.topic)
            payload = message.payload.decode().strip().upper()
            out = command_topics.get(topic_str)
            if out is None:
                continue
            if payload == "ON":
                await self._set_output(out, True)
            elif payload == "OFF":
                await self._set_output(out, False)
            else:
                logging.warning("Unknown payload '%s' on topic %s", payload, topic_str)

    # ── Entry point ───────────────────────────────────────────────────────────

    async def run(self):
        mqtt_cfg = self.cfg["mqtt"]
        logging.info(
            "Connecting to MQTT broker %s:%s",
            mqtt_cfg["host"],
            mqtt_cfg.get("port", 1883),
        )

        async with aiohttp.ClientSession() as http_session:
            self._evok = EvokRest(self.cfg["unipi"]["api_url"], http_session)

            async with aiomqtt.Client(
                hostname=mqtt_cfg["host"],
                port=mqtt_cfg.get("port", 1883),
                username=mqtt_cfg.get("username"),
                password=mqtt_cfg.get("password"),
                identifier=mqtt_cfg.get("client_id", "unipi-mqtt"),
                will=aiomqtt.Will(
                    topic=self.availability_topic,
                    payload=b"offline",
                    retain=True,
                    qos=1,
                ),
            ) as mqtt_client:
                self._mqtt = mqtt_client
                logging.info("MQTT connected")

                await self._mqtt.publish(
                    self.availability_topic, b"online", retain=True, qos=1
                )
                await self._publish_discovery()

                # Run both loops concurrently
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(self._evok_ws_loop())
                    tg.create_task(self._mqtt_command_loop())


# ──────────────────────────────────────────────────────────────────────────────
# Bootstrap
# ──────────────────────────────────────────────────────────────────────────────


def main():
    config_path = Path(__file__).parent / "config.yaml"
    cfg = load_config(str(config_path))

    log_level = cfg.get("logging", {}).get("level", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    bridge = UnipiBridge(cfg)

    loop = asyncio.new_event_loop()

    def _shutdown(sig, frame):
        logging.info("Shutting down (signal %s)…", sig)
        for task in asyncio.all_tasks(loop):
            task.cancel()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        loop.run_until_complete(bridge.run())
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        loop.close()
        logging.info("Stopped")


if __name__ == "__main__":
    main()
