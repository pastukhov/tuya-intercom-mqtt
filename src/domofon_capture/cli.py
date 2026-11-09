"""Capture, MQTT and live HTTP console interface; errors deliberately omit exception payloads."""
import argparse
import asyncio
import json
import logging
import signal
import sys
from pathlib import Path
from .capture import capture, positive_finite, CaptureError
from .config import load_config, ConfigError
from .session import Session, SessionError
from .mqtt_service import MqttError


def positive(value):
  try:
    return positive_finite(value)
  except ValueError:
    raise argparse.ArgumentTypeError('Must be a positive finite number') from None

def parser():
  root = argparse.ArgumentParser(description='Local JVS intercom photo / silent video capture')
  commands = root.add_subparsers(dest='command', required=True)
  for name, ext in [('photo','.jpg'), ('video','.mp4')]:
    p = commands.add_parser(name)
    p.add_argument('--output', type=Path, required=True, help=f'Output {ext} file')
    p.add_argument('--resolution', choices=['sd','hd'], default='sd')
    p.add_argument('--timeout', type=positive, default=30, help='Startup timeout in seconds (default:30)')
    p.add_argument('--env-file', type=Path, default=Path('.env'))
    p.add_argument('--device-config', type=Path)
    p.add_argument('--overwrite', action='store_true')
    if name=='video':
      p.add_argument('--duration', type=positive, default=10)
  p = commands.add_parser('mqtt', help='Publish local events/photos to MQTT / YAIC')
  p.add_argument('--source-id', required=True)
  p.add_argument('--env-file', type=Path, default=Path('.env'))
  p.add_argument('--device-config', type=Path)
  p.add_argument('--mqtt-env-file', type=Path)
  p.add_argument('--mqtt-topic-prefix', default='')
  p.add_argument('--duration', type=positive, help='Optional bounded service runtime in seconds')
  p = commands.add_parser('serve', help='Share one local camera via JPEG / H264 MPEG-TS HTTP')
  p.add_argument('--bind', default='127.0.0.1')
  p.add_argument('--port', type=int, default=19845)
  p.add_argument('--env-file', type=Path, default=Path('.env'))
  p.add_argument('--device-config', type=Path)
  p.add_argument('--duration', type=positive, help='Optional bounded server runtime')
  return root

async def run(args):
  loop = asyncio.get_running_loop()
  loop.add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
  if args.output.suffix.lower() not in (('.jpg','.jpeg') if args.command=='photo' else ('.mp4',)):
    raise ConfigError('Output extension must match requested capture type')
  if not args.overwrite and args.output.exists():
    raise FileExistsError
  config = load_config(args.env_file, args.device_config)
  session = Session(config,args.resolution,args.timeout)
  close_attempted = False
  async def shutdown():
    nonlocal close_attempted
    close_attempted = True
    await session.close()
  try:
    return await capture(session.frames(), args.output, args.command, args.resolution, getattr(args,'duration',10), args.overwrite,args.timeout, before_publish=shutdown)
  finally:
    if not close_attempted:
      await shutdown()

async def run_live(args):
  from .live import LiveService
  if not 1<=args.port<=65535:
    raise ConfigError('Port must be between1 and65535')
  service = LiveService(load_config(args.env_file,args.device_config),bind=args.bind,port=args.port)
  loop = asyncio.get_running_loop()
  stop = asyncio.Event()
  for sig in (signal.SIGINT,signal.SIGTERM):loop.add_signal_handler(sig,stop.set)
  timer = loop.call_later(args.duration,stop.set) if args.duration else None
  try:
    print(json.dumps({'status':'listening','bind':args.bind,'port':service.http.port}),flush=True)
    await service.run(stop)
    return {'status':'stopped','frames':service.frames_received,'reconnects':service.reconnects}
  finally:
    if timer:timer.cancel()
    for sig in (signal.SIGINT,signal.SIGTERM):loop.remove_signal_handler(sig)

def main(argv=None):
  args = parser().parse_args(argv)
  logging.disable(logging.CRITICAL)
  try:
    if args.command == 'serve':
      print(json.dumps(asyncio.run(run_live(args))))
    elif args.command == 'mqtt':
      import threading
      from .mqtt_service import BrokerConfig, MqttService
      stop = threading.Event()
      previous = {sig:signal.signal(sig,lambda *_:stop.set()) for sig in (signal.SIGINT,signal.SIGTERM)}
      timer = threading.Timer(args.duration,stop.set) if args.duration else None
      try:
        if timer:
          timer.start()
        service = MqttService(load_config(args.env_file,args.device_config),args.source_id,BrokerConfig.load(args.mqtt_env_file,args.mqtt_topic_prefix))
        service.run(stop)
        print(json.dumps({'source':args.source_id,'status':'stopped'}))
      finally:
        if timer:
          timer.cancel()
        for sig,handler in previous.items():
          signal.signal(sig,handler)
    else:
      print(json.dumps(asyncio.run(run(args)), ensure_ascii=False))
    return 0
  except (KeyboardInterrupt, asyncio.CancelledError):
    print('Capture interrupted',file=sys.stderr)
    return 130
  except ConfigError as error:
    print(str(error),file=sys.stderr)
  except FileExistsError:
    print('Output already exists; use --overwrite',file=sys.stderr)
  except (SessionError, CaptureError, MqttError) as error:
    print(str(error),file=sys.stderr)
  except Exception:
    print('Capture failed; check configuration, requested resolution, media and output permissions',file=sys.stderr)
  return 1
