"""Read-only LAN3.3 listener, ported from YAIC TuyaSource (2026-11-08)."""
from __future__ import annotations

import logging
from .events import decode_events

logger = logging.getLogger(__name__)
READ_DPS = ('109', '110', '136', '239', '240', '243', '244')


class ConsumerError(RuntimeError):
    pass


class TuyaSource:
    def __init__(self, device_config: dict, source_id: str, *, device_factory=None):
        if not all(isinstance(device_config.get(key), str) and device_config[key] for key in ('id', 'ip', 'local_key')):
            raise ValueError('Device config requires id, ip and local_key strings')
        if str(device_config.get('version', '3.3')) != '3.3':
            raise ValueError('Only verified LAN version 3.3 is supported')
        self.config = device_config
        self.source_id = source_id
        self.device_factory = device_factory

    def run(self, on_event, stop_event):
        factory = self.device_factory
        if factory is None:
            import tinytuya
            # Library debug output contains decrypted private payloads.
            logging.getLogger('tinytuya').disabled = True
            factory = tinytuya.Device
        delay = 1
        while not stop_event.is_set():
            device = None
            try:
                device = factory(self.config['id'], self.config['ip'], self.config['local_key'], dev_type='device22')
                device.set_version(3.3)
                device.set_socketRetryLimit(1)
                device.set_socketTimeout(2)
                device.set_socketPersistent(True)
                device.dps_to_request = dict.fromkeys(READ_DPS)
                response = device.status()
                while not stop_event.is_set():
                    if isinstance(response, dict) and ('Error' in response or 'Err' in response):
                        raise ConnectionError('Tuya transport failure')
                    for event in decode_events(response, self.source_id):
                        try:
                            on_event(event)
                        except Exception:
                            raise ConsumerError('Tuya event consumer failed') from None
                    delay = 1
                    response = device.receive()
                    if response is None:
                        # TinyTuya returns None on ordinary socket timeout. Keep socket.
                        stop_event.wait(.1)
            except ConsumerError:
                raise
            except Exception:
                logger.warning('Tuya connection interrupted; retrying')
                stop_event.wait(delay)
                delay = min(delay * 2, 30)
            finally:
                if device is not None:
                    try:
                        device.close()
                    except Exception:
                        logger.warning('Tuya socket close failed')
