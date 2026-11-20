"""A polling client for the session-owned gameplay module. Reads never take control."""
import asyncio
import contextvars
import json
import os
import re
import time
import urllib.request
import uuid

from .models import SDKError, record
from .protocol import opcode as resolve_opcode

deadline = contextvars.ContextVar("wow_sdk_deadline", default=float("inf"))


class SDK:
    def __init__(self, rpc_url=None, *, mode="control"):
        if mode not in ("control", "observe"):
            raise ValueError("mode must be control or observe")
        self.url = rpc_url or os.environ.get("WOW_SDK_RPC_URL", "http://127.0.0.1:" + os.environ.get("AGENT_WOW_WORLDRPC_PORT", "8086") + "/rpc")
        self.mode, self.token = mode, uuid.uuid4().hex
        self._state, self._received, self._poller = None, None, None
        self._closed, self._error, self._claimed = False, None, False
        self.logs = []

    def _http(self, operation, parameters, timeout):
        body = json.dumps({"jsonrpc": "2.0", "id": uuid.uuid4().hex,
                           "method": "gameplay.execute", "params": {"operation": operation, **parameters}}).encode()
        request = urllib.request.Request(self.url, body, {"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.load(response)
        if "error" in result:
            raise SDKError("rpc_error", result["error"]["message"])
        value = result["result"]
        if value.get("error"):
            raise SDKError(value["error"]["reason"], value["error"]["message"])
        return value

    async def _call(self, operation, *, timeout_s=2, **parameters):
        if self._closed:
            raise SDKError("closed", "Execution has finished")
        remaining = min(timeout_s, deadline.get() - time.monotonic())
        if remaining <= 0:
            raise TimeoutError("Execution deadline exceeded")
        return await asyncio.to_thread(self._http, operation, parameters, remaining)

    async def _start(self, timeout_s):
        end = time.monotonic() + timeout_s
        while True:
            self._state = await self._call("snapshot")
            self._received = time.monotonic()
            if self._state["ready"]:
                break
            if time.monotonic() >= end:
                raise SDKError("not_ready", "Log in a character with the gameplay module installed before running a script")
            await asyncio.sleep(.1)
        self.session_id = self._state["session_id"]
        if self.mode == "control":
            await self._call("acquire", token=self.token)
            self._claimed = True
        self._poller = asyncio.create_task(self._poll())

    async def _poll(self):
        try:
            while True:
                await self._refresh()
                await asyncio.sleep(.1)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._error = error

    async def _refresh(self):
        state = await self._call("snapshot", token=self.token if self._claimed else "")
        if state["session_id"] != self.session_id or not state["ready"]:
            raise SDKError("disconnected", "The character session ended or changed")
        if state["revision"] >= self._state["revision"]:
            self._state = state
        self._received = time.monotonic()

    def _check(self):
        if self._closed:
            raise SDKError("closed", "Execution has finished")
        if self._error:
            raise SDKError("disconnected", str(self._error))
        if self._received is None or time.monotonic() - self._received > 3:
            raise SDKError("stale_state", "No fresh gameplay observations")

    async def _close(self):
        # Revoke the lease before returning: late HTTP requests cannot keep acting.
        self._closed = True
        if self._poller:
            self._poller.cancel()
            await asyncio.gather(self._poller, return_exceptions=True)
        if self._claimed:
            await asyncio.to_thread(self._http, "release", {"token": self.token}, 2)
            self._claimed = False

    def get_state(self):
        self._check()
        return record(self._state)

    def get_player(self): return self.get_state().player
    def get_inventory(self): return self.get_state().inventory
    def get_equipment(self): return self.get_state().equipment
    def get_quest_log(self): return self.get_state().quests
    def get_spells(self): return self.get_state().spells
    def get_dialog(self): return self.get_state().dialog

    def get_connection_status(self):
        age = time.monotonic() - self._received if self._received else None
        connected = not self._closed and self._error is None and age is not None and age <= 3
        return record(dict(connected=connected,
                           ready=connected and bool(self._state and self._state["ready"]), mode=self.mode,
                           state_age_s=age,
                           error=str(self._error) if self._error else None))

    @staticmethod
    def _matches(value, query, *, key="name"):
        if query is None:
            return True
        if isinstance(query, dict):
            identifier = query.get("guid", query.get("id"))
            return identifier is not None and value.get("guid", value.get("id")) == identifier
        if isinstance(query, int):
            return value.get("id", value.get("entry_id")) == query
        name = value.get(key) or ""
        return bool(query.search(name)) if isinstance(query, re.Pattern) else name.casefold() == str(query).casefold()

    def _find_entity(self, kind, query, entry_id, radius, alive=None):
        candidates = [e for e in self.get_state().entities if e.kind == kind and self._matches(e, query)
                      and (entry_id is None or e.entry_id == entry_id) and e.distance is not None and e.distance <= radius
                      and (alive is None or e.alive is alive)]
        return min(candidates, key=lambda e: (e.distance, e.guid), default=None)

    def find_npc(self, query=None, *, entry_id=None, radius=50, alive=None):
        return self._find_entity("npc", query, entry_id, radius, alive)

    def find_object(self, query=None, *, entry_id=None, radius=50):
        return self._find_entity("object", query, entry_id, radius)

    def find_item(self, query=None, *, item_id=None, equipped=False):
        items = list(self.get_equipment().values()) if equipped else self.get_inventory().items
        return next((item for item in items if self._matches(item, query)
                     and (item_id is None or item.id == item_id)), None)

    def find_spell(self, query, *, rank=None):
        matches = [s for s in self.get_spells() if self._matches(s, query)
                   and (rank is None or s.rank == rank or s.rank_name == rank)]
        return max(matches, key=lambda s: (s.rank or 0, s.id), default=None)

    async def wait_until(self, predicate, *, timeout_s=10):
        async with asyncio.timeout(timeout_s):
            while True:
                state = self.get_state()
                if predicate(state):
                    return state
                await asyncio.sleep(.05)

    async def _query(self, kind, identifier, timeout_s):
        result = await self._call("query", kind=kind, id=int(identifier), timeout_s=min(2, timeout_s))
        cursor = result["revision"]
        async with asyncio.timeout(timeout_s):
            while True:
                state = self.get_state()
                if kind == "completed":
                    if state.completed_revision > cursor:
                        return state.completed_quests
                else:
                    info = state.metadata[kind].get(str(identifier))
                    if info is not None and "exists" in info:
                        return info
                await asyncio.sleep(.05)

    async def item_info(self, item_id, *, timeout_s=5):
        return await self._query("item", item_id, timeout_s)

    async def quest_info(self, quest_id, *, timeout_s=5):
        return await self._query("quest", quest_id, timeout_s)

    async def completed_quests(self, *, timeout_s=5):
        return await self._query("completed", 0, timeout_s)

    async def _command(self, operation, **parameters):
        self._check()
        if self.mode != "control" or not self._claimed:
            raise SDKError("read_only", "Observe mode cannot perform game actions")
        return await self._call(operation, token=self.token, **parameters)

    async def send_packet(self, opcode, payload, *, timeout_s=5):
        if not isinstance(payload, bytes):
            raise TypeError("payload must be bytes")
        return record(await self._command("send", opcode=resolve_opcode(opcode),
                                          payload_hex=payload.hex(), timeout_s=timeout_s))

    async def _stop(self):
        # Stopping is also allowed during deadline/cancellation cleanup.
        if self._claimed:
            await asyncio.to_thread(self._http, "stop", {"token": self.token}, 1)
