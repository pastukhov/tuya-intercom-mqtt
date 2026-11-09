"""Bounded read-only intercom events and local photos to MQTT / YAIC."""
import asyncio
import base64
import json
import logging
import os
import queue
import re
import ssl
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from .capture import capture
from .session import Session
from .source import TuyaSource

class MqttError(RuntimeError):
  pass

def env_values(path=None):
  values = {}
  if path:
    for line in Path(path).read_text().splitlines():
      line = line.strip()
      if not line or line.startswith('#'):
        continue
      key, sep, value = line.partition('=')
      if not sep:
        raise ValueError('Invalid env assignment')
      value = value.strip()
      if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
      values[key.strip()] = value
  values.update(os.environ)
  return values

@dataclass(frozen=True)
class BrokerConfig:
  host: str
  port: int = 1883
  prefix: str = ''
  image_template: str = 'yaic/input/+/image'
  username: str = field(default='', repr=False)
  password: str = field(default='', repr=False)
  tls: bool = False

  @classmethod
  def load(cls, path=None, prefix=''):
    values = env_values(path)
    host = values.get('MQTT_HOST', '')
    port = int(values.get('MQTT_PORT','1883'))
    template = values.get('MQTT_TOPIC_IN','yaic/input/+/image')
    if not host or not 1 <= port <= 65535 or template.split('/').count('+') != 1 or '#' in template:
      raise ValueError('Invalid MQTT host, port or input template')
    if any(c in prefix for c in '+#\x00') or prefix.startswith('/') or prefix.endswith('/'):
      raise ValueError('Invalid MQTT topic prefix')
    return cls(host,port,prefix,template,values.get('MQTT_USERNAME',''),values.get('MQTT_PASSWORD',''),values.get('MQTT_TLS','false').lower()=='true')

class Publisher:
  def __init__(self, broker, status_topic):
    import paho.mqtt.client as mqtt
    self.mqtt = mqtt
    self.ready = threading.Event()
    self.connected = False
    self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,client_id='domofon-'+uuid.uuid4().hex[:16],reconnect_on_failure=False)
    self.on_failure = None
    self.failed = False
    self.closing = False
    self.client.max_queued_messages_set(16)
    self.client.max_inflight_messages_set(4)
    self.client.reconnect_delay_set(1,30)
    if broker.username:
      self.client.username_pw_set(broker.username,broker.password)
    if broker.tls:
      self.client.tls_set_context(ssl.create_default_context())
    self.client.will_set(status_topic,json.dumps({'status':'offline'}),qos=1,retain=True)
    def connected(client,userdata,flags,reason,properties):
      self.connected = not reason.is_failure and not self.failed
      self.ready.set()
    def disconnected(client,userdata,flags,reason,properties):
      self.connected = False
      if not self.closing:
        self.failed = True
        if self.on_failure is not None:
          self.on_failure()
    self.client.on_connect = connected
    self.client.on_disconnect = disconnected
    self.broker = broker
  def start(self):
    self.client.connect_async(self.broker.host,self.broker.port,keepalive=30)
    self.client.loop_start()
    if not self.ready.wait(10) or not self.connected:
      self.close()
      raise MqttError('MQTT connection failed or timed out')
  def publish(self, topic, payload, *, retain=False):
    if not self.connected:
      raise MqttError('MQTT disconnected')
    info = self.client.publish(topic,payload,qos=1,retain=retain)
    if info.rc != self.mqtt.MQTT_ERR_SUCCESS:
      raise MqttError('MQTT publish queue unavailable')
    try:
      info.wait_for_publish(timeout=5)
      if not info.is_published():
        raise MqttError('MQTT publish acknowledgement timed out')
    except (RuntimeError,ValueError):
      self.connected = False
      self.failed = True
      self.client.disconnect()
      if self.on_failure is not None:
        self.on_failure()
      raise MqttError('MQTT publication failed') from None
  def close(self):
    self.closing = True
    self.client.disconnect()
    self.client.loop_stop()

class MqttService:
  def __init__(self, config, source_id, broker, *, publisher=None, capture=None, clock=time.monotonic):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',source_id):
      raise ValueError('Invalid source ID')
    self.config, self.source_id, self.broker = config,source_id,broker
    root = broker.prefix + '/' if broker.prefix else ''
    self.event_topic = root + f'domofon/{source_id}/event'
    self.status_topic = root + f'domofon/{source_id}/status'
    self.image_topic = root + broker.image_template.replace('+',source_id)
    self.publisher = publisher or Publisher(broker,self.status_topic)
    self.capture = capture or self.capture_photo
    self.clock = clock
    self.jobs = queue.Queue(maxsize=8)
    self.seen = OrderedDict()
    self.rings = {}
    self.stop = threading.Event()
    self.failure = None
    self.publisher.on_failure = self._broker_failed
  def _broker_failed(self):
    self.failure = MqttError('MQTT disconnected; restart service after broker recovery')
    self.stop.set()
  def publish(self, topic, obj, *, retain=False):
    self.publisher.publish(topic,json.dumps(obj,separators=(',',':')),retain=retain)
  def on_event(self, event):
    now = self.clock()
    while self.seen and next(iter(self.seen.values())) <= now-30:
      self.seen.popitem(last=False)
    key = (event.kind,event.dedup_key,event.panel)
    ring_change = event.kind in ('doorbell_ring','doorbell_idle') and self.rings.get(event.panel) != event.kind
    if key in self.seen and not ring_change:
      return
    if event.kind in ('doorbell_ring','doorbell_idle'):
      self.rings[event.panel] = event.kind
    self.seen[key] = now
    if len(self.seen)>256:
      self.seen.popitem(last=False)
    event_id = uuid.uuid4().hex
    payload = dict(event.to_payload(),event_id=event_id)
    self.publish(self.event_topic,payload)
    if event.kind not in ('human_detected','doorbell_ring'):
      return
    try:
      self.jobs.put_nowait((payload,now))
    except queue.Full:
      self.unavailable(payload,'queue_full')
  def unavailable(self, payload, reason):
    self.publish(self.event_topic,{'event_type':'snapshot_unavailable','source':self.source_id,'event_id':payload['event_id'],'reason':reason})
  def capture_photo(self):
    async def run():
      session = Session(self.config,'sd',30)
      closed = False
      async def shutdown():
        nonlocal closed
        closed = True
        await session.close()
      with tempfile.TemporaryDirectory(prefix='domofon-mqtt-') as directory:
        output = Path(directory)/'photo.jpg'
        try:
          await capture(session.frames(),output,'photo','sd',before_publish=shutdown)
          return output.read_bytes()
        finally:
          if not closed:
            await shutdown()
    return asyncio.run(asyncio.wait_for(run(),40))
  def worker(self):
    while True:
      job = self.jobs.get()
      try:
        if job is None:
          return
        payload, queued_at = job
        if self.stop.is_set():
          self.unavailable(payload,'shutdown')
          continue
        if self.clock()-queued_at > 5:
          self.unavailable(payload,'snapshot_job_expired')
          continue
        try:
          image = self.capture()
        except Exception:
          self.unavailable(payload,'capture_failed')
          continue
        if self.stop.is_set():
          self.unavailable(payload,'shutdown')
          continue
        self.publish(self.image_topic,{'image_b64':base64.b64encode(image).decode(),'device':self.source_id,'event_id':payload['event_id']},retain=False)
      except Exception:
        self.failure = MqttError('MQTT image/event delivery failed')
        self.stop.set()
      finally:
        self.jobs.task_done()
  def run(self, stop=None, *, listener=None):
    if stop is not None:
      self.stop = stop
    self.publisher.on_failure = self._broker_failed
    self.publisher.start()
    worker = threading.Thread(target=self.worker,name='domofon-capture',daemon=True)
    try:
      self.publish(self.status_topic,{'status':'online'},retain=True)
      worker.start()
      listener = listener or TuyaSource({'id':self.config.device_id,'ip':self.config.ip,'local_key':self.config.local_key},self.source_id)
      listener.run(self.on_event,self.stop)
    finally:
      self.stop.set()
      if worker.ident is not None:
        self.jobs.put(None)
        worker.join(timeout=50)
      try:
        self.publish(self.status_topic,{'status':'offline'},retain=True)
      except Exception:
        pass
      self.publisher.close()
    if worker.is_alive():
      raise MqttError('Capture worker shutdown timed out')
    if self.failure:
      raise self.failure
    if getattr(self.publisher,'failed',False):
      raise MqttError('MQTT disconnected; restart service after broker recovery')
