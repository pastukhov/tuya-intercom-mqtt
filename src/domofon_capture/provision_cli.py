"""Offline provisioning from an owner's completed Smart Life LAN capture."""
import argparse
import os
from pathlib import Path
from .config import Config
from .provision import capture_material


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--capture', type=Path, required=True)
    p.add_argument('--env-file', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    try:
        values = {}
        for line in a.env_file.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith('#'):
                k, v = line.split('=', 1)
                values[k.strip()] = v.strip().strip('\"\'')
        cfg = Config(values['TUYA_DEVICE_ID'], values['TUYA_DEVICE_IP'], values['TUYA_LOCAL_KEY'], '')
        material = capture_material(a.capture, config=cfg, decode_video=False)
        fd = os.open(a.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write('TUYA_IMM_AUTH=' + material.credential + '\n')
        print('IMM credential saved to the requested file (mode 0600).')
        return 0
    except Exception:
        print('Provisioning failed: check capture, configuration and tshark; output must not exist.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
