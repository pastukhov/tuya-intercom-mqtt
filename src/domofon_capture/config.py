"""Credential loading without disclosure or mutation."""
import ipaddress
import json
import os
from dataclasses import dataclass
from pathlib import Path

class ConfigError(ValueError):
  pass

@dataclass(frozen=True, repr=False)
class Config:
  device_id: str
  ip: str
  local_key: str
  rtc_auth: str

  def __repr__(self):
    return 'Config(<redacted>)'

def load_config(env_file=Path('.env'), device_config=None, environ=None):
  values = {}
  try:
    if device_config:
      legacy = json.loads(Path(device_config).read_text())
      for old, new in [('id','DEVICE_ID'), ('ip','DEVICE_IP'), ('local_key','LOCAL_KEY')]:
        if old in legacy:
          values['TUYA_' + new] = legacy[old]
    if env_file and Path(env_file).exists():
      for line in Path(env_file).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
          continue
        name, sep, value = line.partition('=')
        if not sep:
          raise ConfigError('Invalid env file assignment')
        values[name.strip()] = value.strip().strip('\"\'')
    values.update(os.environ if environ is None else environ)
    fields = [values.get('TUYA_' + k, '') for k in ('DEVICE_ID','DEVICE_IP','LOCAL_KEY','RTC_AUTH')]
    if any(not isinstance(v, str) or not v.strip() for v in fields):
      raise ConfigError('Missing required TUYA_DEVICE_ID, TUYA_DEVICE_IP, TUYA_LOCAL_KEY or TUYA_RTC_AUTH')
    ipaddress.IPv4Address(fields[1])
    if len(fields[2].encode()) != 16:
      raise ConfigError('TUYA_LOCAL_KEY must be exactly 16 bytes')
    return Config(*fields)
  except ConfigError:
    raise
  except (OSError, ValueError, TypeError, AttributeError):
    raise ConfigError('Cannot read or validate device configuration') from None
