"""WebSocket connection manager with per-household broadcasting."""
import asyncio
import logging
import time
from fastapi import WebSocket
from typing import Dict, List

logger = logging.getLogger("huddle")


class ConnectionManager:
    # Run stale-connection cleanup at least every 5 minutes, regardless of
    # connection count. The old threshold of 100 was arbitrary and meant
    # cleanup never ran on smaller deployments.
    CLEANUP_INTERVAL_SECONDS = 300  # 5 minutes

    def __init__(self):
        # Map household_id -> list of WebSocket connections
        self.connections: Dict[int, List[WebSocket]] = {}
        # Also track legacy connections (no household_id) for backwards compat during migration
        self.legacy_connections: List[WebSocket] = []
        self._log_task = None
        self._last_cleanup_time: float = 0.0

    async def connect(self, websocket: WebSocket, household_id: int = None):
        await websocket.accept()
        if household_id is not None:
            if household_id not in self.connections:
                self.connections[household_id] = []
            self.connections[household_id].append(websocket)
        else:
            self.legacy_connections.append(websocket)

        # Start the periodic log task if not already running
        if self._log_task is None or self._log_task.done():
            self._log_task = asyncio.create_task(self._periodic_log())

    def disconnect(self, websocket: WebSocket, household_id: int = None):
        if household_id is not None:
            if household_id in self.connections:
                if websocket in self.connections[household_id]:
                    self.connections[household_id].remove(websocket)
                # Clean up empty household lists
                if not self.connections[household_id]:
                    del self.connections[household_id]
        else:
            if websocket in self.legacy_connections:
                self.legacy_connections.remove(websocket)

    def _total_connections(self) -> int:
        total = len(self.legacy_connections)
        for conns in self.connections.values():
            total += len(conns)
        return total

    async def _cleanup_stale(self):
        """Remove stale connections.

        Triggered when total connections exceed 100 OR when the periodic
        timer fires (every CLEANUP_INTERVAL_SECONDS). This ensures stale
        connections are cleaned up even on small deployments.
        """
        total = self._total_connections()
        if total == 0:
            return

        logger.info("WebSocket cleanup: checking %d connections for stale entries", total)
        removed = 0

        # Check legacy connections
        alive = []
        for ws in self.legacy_connections:
            try:
                await asyncio.wait_for(ws.send_json({"type": "ping"}), timeout=5)
                alive.append(ws)
            except Exception:
                removed += 1
        self.legacy_connections = alive

        # Check household connections
        for hid in list(self.connections.keys()):
            alive = []
            for ws in self.connections[hid]:
                try:
                    await asyncio.wait_for(ws.send_json({"type": "ping"}), timeout=5)
                    alive.append(ws)
                except Exception:
                    removed += 1
            if alive:
                self.connections[hid] = alive
            else:
                del self.connections[hid]

        if removed:
            logger.info("WebSocket cleanup: removed %d stale connections", removed)

    async def broadcast(self, message: dict, household_id: int = None):
        """Broadcast to a specific household, or all connections if no household_id."""
        targets = []
        if household_id is not None:
            targets = self.connections.get(household_id, [])[:]
        else:
            # Broadcast to ALL connections (legacy behavior)
            targets = self.legacy_connections[:]
            for conns in self.connections.values():
                targets.extend(conns)

        failed = []
        for connection in targets:
            try:
                await connection.send_json(message)
            except Exception as e:
                logger.debug("WebSocket send failed, removing connection: %s", e)
                failed.append(connection)

        # Clean up failed connections
        for ws in failed:
            if ws in self.legacy_connections:
                self.legacy_connections.remove(ws)
            for hid in list(self.connections.keys()):
                if ws in self.connections[hid]:
                    self.connections[hid].remove(ws)
                    if not self.connections[hid]:
                        del self.connections[hid]

    async def _periodic_log(self):
        """Log connection counts every 60 seconds and run periodic stale cleanup."""
        while True:
            try:
                await asyncio.sleep(60)
                total = self._total_connections()
                if total > 0:
                    per_household = {hid: len(conns) for hid, conns in self.connections.items()}
                    logger.debug(
                        "WebSocket connections: %d total, legacy=%d, per_household=%s",
                        total, len(self.legacy_connections), per_household,
                    )

                # Run stale cleanup if count exceeds 100 OR every 5 minutes
                now = time.time()
                time_since_cleanup = now - self._last_cleanup_time
                if total > 100 or (total > 0 and time_since_cleanup >= self.CLEANUP_INTERVAL_SECONDS):
                    self._last_cleanup_time = now
                    await self._cleanup_stale()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("WebSocket periodic log error (will retry): %s", e)


manager = ConnectionManager()
