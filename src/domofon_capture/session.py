"""Ephemeral LAN signalling, local STUN and fingerprint-verified WebRTC."""
import asyncio
import hashlib
import hmac
import json
import socket
import struct
import types
import uuid
from contextlib import asynccontextmanager
from OpenSSL import crypto
from aiortc import RTCPeerConnection, RTCConfiguration, RTCSessionDescription, RTCBundlePolicy
from aiortc.sdp import candidate_from_sdp
from aiortc.rtcdtlstransport import State, X509_DIGEST_ALGORITHMS
from tinytuya.core import AESCipher
from .lan import encode_frame, read_frame, decode_signal

class SessionError(RuntimeError):
  pass

def fingerprints_match(der, fingerprints):
  supported = [f for f in fingerprints if f.algorithm.lower() in X509_DIGEST_ALGORITHMS]
  return bool(supported) and all(hmac.compare_digest(f.value.upper(), ':'.join(
    f'{b:02X}' for b in hashlib.new(f.algorithm.lower().replace('-', ''), der).digest())) for f in supported)

def validate_legacy_certificate(self, parameters):
  cert = self._ssl.get_peer_certificate()
  if cert is None or not fingerprints_match(crypto.dump_certificate(crypto.FILETYPE_ASN1, cert), parameters.fingerprints):
    self._set_state(State.FAILED)

class LocalStun(asyncio.DatagramProtocol):
  def __init__(self, ip):
    self.ip = ip
  def connection_made(self, transport):
    self.transport = transport
  def datagram_received(self, data, peer):
    if peer[0] != self.ip or len(data) < 20 or data[:2] != b'\x00\x01' or data[4:8] != b'\x21\x12\xa4\x42':
      return
    mapped = struct.pack('>BBHI', 0, 1, peer[1] ^ 0x2112, int.from_bytes(socket.inet_aton(peer[0]), 'big') ^ 0x2112a442)
    body = struct.pack('>HH', 0x20, 8) + mapped
    self.transport.sendto(struct.pack('>HHI', 0x101, len(body), 0x2112a442) + data[8:20] + body, peer)

class Session:
  def __init__(self, config, resolution='sd', timeout=30):
    self.config, self.resolution, self.timeout = config, resolution, timeout
    self.pc = RTCPeerConnection(RTCConfiguration(iceServers=[], bundlePolicy=RTCBundlePolicy.MAX_BUNDLE))
    self.writer = self.stun = None
    self.tasks = []
    self.track = asyncio.get_running_loop().create_future()
    self.failure = asyncio.get_running_loop().create_future()
    self.sequence = 0
    self.header = {'from':uuid.uuid4().hex, 'to':config.device_id, 'moto_id':'', 'path':'lan', 'sessionid':uuid.uuid4().hex, 'trace_id':str(uuid.uuid4()).upper(), 'is_pre':0}
    self.send_lock = asyncio.Lock()
    @self.pc.on('track')
    def track_received(track):
      if track.kind == 'video' and not self.track.done():
        self.track.set_result(track)
      elif track.kind == 'audio':
        self.tasks.append(asyncio.create_task(self.discard_audio(track)))
    @self.pc.on('connectionstatechange')
    async def state():
      if self.pc.connectionState == 'failed':
        self.fail('WebRTC connection or certificate authentication failed')
      elif self.pc.connectionState == 'connected' and self.resolution == 'hd':
        try:
          await self.send('resolution', {'mode':'webrtc','cmdValue':0})
        except Exception:
          self.fail('Resolution negotiation failed')
  async def discard_audio(self, track):
    try:
      while True:
        await track.recv()
    except asyncio.CancelledError:
      raise
    except Exception:
      return
  def fail(self, message):
    if not self.failure.done():
      self.failure.set_result(message)
  async def send(self, kind, msg):
    async with self.send_lock:
      self.sequence += 1
      header = dict(self.header, type=kind)
      obj = {'header':header, 'msg':msg}
      data = json.dumps(obj, separators=(',', ':')).encode()
      self.writer.write(encode_frame(self.sequence, 32, AESCipher(self.config.local_key.encode()).encrypt(data, False)))
      await self.writer.drain()
  async def receive(self):
    try:
      while True:
        command, body = await read_frame(self.reader)
        if command != 32:
          continue
        obj = decode_signal(body, self.config.local_key.encode())
        h, msg = obj['header'], obj['msg']
        if h.get('sessionid') != self.header['sessionid']:
          continue
        kind = h.get('type')
        if kind == 'answer':
          await self.pc.setRemoteDescription(RTCSessionDescription(sdp=msg['sdp'], type='answer'))
        elif kind == 'candidate' and msg.get('candidate'):
          candidate = candidate_from_sdp(msg['candidate'].strip().removeprefix('a=').removeprefix('candidate:'))
          if candidate.ip == self.config.ip:
            candidate.sdpMid = self.pc.getTransceivers()[0].mid
            await self.pc.addIceCandidate(candidate)
        elif kind == 'disconnect':
          self.fail('Camera closed the session (check RTC authentication)')
          return
    except asyncio.CancelledError:
      raise
    except Exception:
      self.fail('LAN signalling failed (check connection and credentials)')
  async def start(self):
    self.reader, self.writer = await asyncio.open_connection(self.config.ip, 6668)
    local_ip = self.writer.get_extra_info('sockname')[0]
    self.stun, _ = await asyncio.get_running_loop().create_datagram_endpoint(lambda:LocalStun(self.config.ip), local_addr=(local_ip,0))
    self.pc.addTransceiver('audio', direction='recvonly')
    self.pc.addTransceiver('video', direction='recvonly')
    # Instance-local override: never patch aiortc classes or installed files.
    for transceiver in self.pc.getTransceivers():
      transport = transceiver.receiver.transport
      transport._validate_peer_identity = types.MethodType(validate_legacy_certificate, transport)
    await self.pc.setLocalDescription(await self.pc.createOffer())
    lines = self.pc.localDescription.sdp.splitlines()
    candidates = [s for s in lines if s.startswith('a=candidate:') and local_ip in s]
    sdp = '\r\n'.join(s for s in lines if not s.startswith(('a=candidate:', 'a=end-of-candidates', 'a=extmap:', 'a=fingerprint:sha-384', 'a=fingerprint:sha-512'))) + '\r\n'
    await self.send('offer', {'mode':'webrtc', 'auth':self.config.rtc_auth, 'stream_type':0, 'datachannel_enable':False, 'sdp':sdp, 'token':[{'urls':f'stun:{local_ip}:{self.stun.get_extra_info("sockname")[1]}'}]})
    for candidate in dict.fromkeys(candidates):
      await self.send('candidate', {'mode':'webrtc','candidate':candidate+'\r\n'})
    self.tasks.append(asyncio.create_task(self.receive()))
  async def guarded(self, awaitable, timeout):
    task = asyncio.ensure_future(awaitable)
    try:
      done, _ = await asyncio.wait([task,self.failure], timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
      if self.failure in done:
        raise SessionError(self.failure.result())
      if task not in done:
        raise SessionError('Camera startup or media stall timeout')
      return task.result()
    finally:
      if not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
  async def frames(self):
    deadline = asyncio.get_running_loop().time() + self.timeout
    await asyncio.wait_for(self.start(), self.timeout)
    track = await self.guarded(asyncio.shield(self.track), max(.01, deadline-asyncio.get_running_loop().time()))
    first = True
    while True:
      try:
        frame = await self.guarded(track.recv(), max(.01, deadline-asyncio.get_running_loop().time()) if first else 5)
      except SessionError:
        raise
      except Exception:
        raise SessionError('Camera media ended before capture completed') from None
      first = False
      yield frame
  async def close(self):
    # Shield the complete bounded teardown, including disconnect. Repeated signals
    # defer cancellation until local sockets and the peer have been closed.
    if getattr(self, '_closing_task', None) is None:
      self._closing_task = asyncio.create_task(self._close_resources())
    cancelled = False
    while not self._closing_task.done():
      try:
        await asyncio.shield(self._closing_task)
      except asyncio.CancelledError:
        cancelled = True
    if cancelled:
      # Retrieve any teardown exception before propagating the interruption.
      if not self._closing_task.cancelled():
        self._closing_task.exception()
      raise asyncio.CancelledError
    self._closing_task.result()

  async def _close_resources(self):
    try:
      if self.writer:
        try:
          await asyncio.wait_for(self.send('disconnect', {'mode':'webrtc','close_reason':4,'close_reason_local':0}), 1)
        except Exception:
          pass
      for task in self.tasks:
        task.cancel()
      if self.tasks:
        done, _ = await asyncio.wait(self.tasks, timeout=1)
        for task in done:
          if not task.cancelled():
            task.exception()
      await asyncio.wait_for(self.pc.close(), 3)
    finally:
      # Socket closure is synchronous and runs even if peer closure fails.
      if self.writer:
        self.writer.close()
      if self.stun:
        self.stun.close()
      if self.writer:
        try:
          await asyncio.wait_for(self.writer.wait_closed(), 1)
        except Exception:
          pass


def media_session(config, resolution='hd', timeout=30):
    if getattr(config, 'imm_auth', ''):
        from .imm import IMMSession
        return IMMSession(config)
    return Session(config, resolution, timeout)
