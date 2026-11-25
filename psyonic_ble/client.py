"""Async BLE connection to one Ability Hand (bleak), with request/reply helpers."""

import asyncio
import re
import sys
import time

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from .protocol import NUS_RX, NUS_SERVICE, NUS_TX, STREAM_STOP, printable

NAME_RE = re.compile(r"psy|ability", re.I)


def is_hand(dev: BLEDevice, adv: AdvertisementData, name_filter: str | None = None) -> bool:
    name = adv.local_name or dev.name or ""
    if name_filter:
        return name_filter.lower() in name.lower()
    return NUS_SERVICE in [u.lower() for u in adv.service_uuids] or bool(NAME_RE.search(name))


async def scan(timeout: float = 5.0, name_filter: str | None = None):
    """All hands in range as (device, name, rssi), strongest first."""
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    hands = [(d, a.local_name or d.name or "?", a.rssi) for d, a in found.values() if is_hand(d, a, name_filter)]
    return sorted(hands, key=lambda h: -h[2])


async def find(address: str | None, name_filter: str | None, timeout: float) -> BLEDevice:
    """Find one hand: by address, by name substring, or the first one that advertises."""
    if address:
        dev = await BleakScanner.find_device_by_address(address, timeout=timeout)
    else:
        dev = await BleakScanner.find_device_by_filter(lambda d, a: is_hand(d, a, name_filter), timeout=timeout)
    if dev is None:
        what = address or (f"name containing {name_filter!r}" if name_filter else "any Ability Hand")
        raise ConnectionError(f"no hand found ({what}). Is it powered and not connected to the PSYONIC app?")
    return dev


class Hand:
    """
    Connected hand. Every notification goes into a queue; request() writes a command and
    collects the replies that arrive within a short window.
    """

    def __init__(self, device: BLEDevice, verbose: bool = False):
        self.device = device
        self.verbose = verbose
        self.client = BleakClient(device)
        self.queue: asyncio.Queue[tuple[float, bytes]] = asyncio.Queue()
        self._with_response = False

    @property
    def name(self) -> str:
        return self.device.name or self.device.address

    async def __aenter__(self):
        await self.client.connect()
        rx = self.client.services.get_characteristic(NUS_RX)
        if rx is None:
            await self.client.disconnect()
            raise ConnectionError(f"{self.name} has no Nordic UART service; not an Ability Hand?")
        self._with_response = "write-without-response" not in rx.properties
        await self._acquire_mtu()
        await self.client.start_notify(NUS_TX, self._on_notify)
        return self

    async def __aexit__(self, *exc):
        try:
            if self.client.is_connected:
                await self.client.stop_notify(NUS_TX)
        finally:
            await self.client.disconnect()

    async def _acquire_mtu(self):
        """BlueZ only reports the negotiated MTU after an explicit acquire; others know it already."""
        acquire = getattr(getattr(self.client, "_backend", None), "_acquire_mtu", None)
        if acquire is not None:
            try:
                await acquire()
            except Exception:
                pass   # fall back to the default MTU (23)

    def _on_notify(self, _, data: bytearray):
        if self.verbose:
            print(f"  <- {printable(bytes(data))}", file=sys.stderr)
        self.queue.put_nowait((time.monotonic(), bytes(data)))

    @property
    def max_write(self) -> int:
        return self.client.mtu_size - 3

    def drain(self) -> list[bytes]:
        out = []
        while not self.queue.empty():
            out.append(self.queue.get_nowait()[1])
        return out

    async def send(self, cmd: str | bytes):
        data = cmd.encode("ascii") if isinstance(cmd, str) else cmd
        if len(data) > self.max_write:
            raise ValueError(f"command is {len(data)} bytes but the BLE link only carries {self.max_write} "
                             f"(MTU {self.client.mtu_size})")
        if self.verbose:
            print(f"  -> {printable(data)}", file=sys.stderr)
        await self.client.write_gatt_char(NUS_RX, data, response=self._with_response)

    async def request(self, cmd: str | bytes, wait: float = 1.0, settle: float = 0.15,
                      until: re.Pattern | str | None = None) -> list[str]:
        """
        Send cmd and return the text replies. Waits up to `wait` s for the first reply, then
        keeps collecting until nothing arrives for `settle` s, or until a reply matches `until`.
        """
        self.drain()
        await self.send(cmd)
        if isinstance(until, str):
            until = re.compile(until)
        replies: list[str] = []
        deadline = time.monotonic() + wait
        while True:
            timeout = deadline - time.monotonic() if not replies else settle
            if timeout <= 0:
                break
            try:
                _, data = await asyncio.wait_for(self.queue.get(), timeout)
            except asyncio.TimeoutError:
                break
            text = printable(data)
            replies.append(text)
            if until is not None and until.search(text):
                break
        return replies

    async def stream(self, cmd: str, duration: float | None = None):
        """Start a 'P' stream and yield (t, raw_bytes) until duration elapses. Always stops the stream."""
        self.drain()
        await self.send(cmd)
        t0 = time.monotonic()
        try:
            while duration is None or time.monotonic() - t0 < duration:
                remaining = None if duration is None else duration - (time.monotonic() - t0)
                try:
                    ts, data = await asyncio.wait_for(self.queue.get(), remaining if remaining else 1.0)
                except asyncio.TimeoutError:
                    continue
                yield ts - t0, data
        finally:
            await self.stop_stream()

    async def stop_stream(self):
        if self.client.is_connected:
            await self.send(STREAM_STOP)
            await asyncio.sleep(0.2)
            self.drain()
