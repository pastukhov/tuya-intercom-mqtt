"""Known notification decoder, ported from YAIC tuya_events (2026-11-08).
No runtime dependency on YAIC; private snapshot URLs are never published.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit


@dataclass(frozen=True)
class TuyaEvent:
    kind: str
    source_id: str
    received_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    device_timestamp: int | float | None = None
    panel: int | None = None
    snapshot_url: str | None = field(default=None, repr=False)
    dedup_key: str = field(default='', repr=False)

    def to_payload(self) -> dict:
        result = {'event_type': self.kind, 'source': self.source_id, 'received_at': self.received_at, 'timestamp': self.received_at}
        if self.panel is not None:
            result['panel'] = self.panel
        if self.device_timestamp is not None:
            result['device_timestamp'] = self.device_timestamp
        return result


def _decode(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 131072:
        return None
    try:
        return base64.b64decode(value, validate=True).decode('utf-8')
    except (ValueError, binascii.Error, UnicodeError):
        return None


def decode_events(response: object, source_id: str) -> list[TuyaEvent]:
    if not isinstance(response, dict) or 'Error' in response or 'Err' in response:
        return []
    dps = response.get('dps')
    if not isinstance(dps, dict):
        return []
    events = []
    stamp = response.get('t')
    if type(stamp) not in (int, float) or not math.isfinite(stamp):
        stamp = None

    def event(kind, content, url=None, panel=None):
        fingerprint = hashlib.sha256(json.dumps([kind, content, stamp, panel], sort_keys=True).encode()).hexdigest()
        events.append(TuyaEvent(kind, source_id, device_timestamp=stamp, panel=panel, snapshot_url=url, dedup_key=fingerprint))

    for dp in ('239', '240'):
        value = dps.get(dp)
        if value in ('Ring', 'Normal'):
            event('doorbell_ring' if value == 'Ring' else 'doorbell_idle', value, panel=int(dp) - 238)
    human = _decode(dps.get('185'))
    if human:
        try:
            data = json.loads(human)
        except (ValueError, RecursionError):
            data = None
        if isinstance(data, dict) and data.get('cmd') == 'ipc_human':
            event('human_detected', human)
    url = _decode(dps.get('154'))
    if url:
        try:
            parts = urlsplit(url)
            if parts.scheme == 'https' and parts.hostname and not parts.username and not parts.password:
                event('doorbell_image', url, url)
        except ValueError:
            pass
    return events
