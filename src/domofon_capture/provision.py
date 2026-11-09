"""Offline LAN32/IMM decoder. Credentials and media remain in RAM; no network IO."""
import collections
import hashlib
import hmac
import importlib
import json
import math
import re
import struct
import subprocess
import sys
import types
import zlib
from dataclasses import dataclass
from pathlib import Path
from .config import load_config
from tinytuya.core import AESCipher

class CaptureDecodeError(ValueError):
  pass

@dataclass(repr=False)
class CaptureMaterial:
  credential: str
  summary: dict
  video_annexb: bytes

  def __repr__(self):
    return 'CaptureMaterial(<redacted>)'

def reference_crypto():
  from . import imm_crypto
  return imm_crypto


def summarize_control_payload(command, sub, payload):
  """Only known numeric session controls and non-secret capability fields."""
  if command == 6 and sub in (0, 4) and len(payload) == 8:
    return {'uint32_values': list(struct.unpack('<II', payload))}
  if command == 10 and sub == 0 and len(payload) == 4:
    return {'uint32_values': [int.from_bytes(payload, 'little')]}
  if command != 21:
    return {}
  if len(payload) == 8:
    return {'uint32_values': list(struct.unpack('<II', payload))}
  try:
    obj = json.loads(payload.rstrip(b'\x00'))
  except (ValueError, UnicodeError):
    return {}
  if not isinstance(obj, dict) or obj.get('cmd') not in (
      'capability_exchange_req', 'capability_exchange_rsp', 'capability_exchange_resp'):
    return {}

  def numeric_tree(value, depth=0):
    if depth > 5:
      return None
    if type(value) in (int, bool) and 0 <= value <= 65535:
      return value
    if isinstance(value, dict):
      public = {}
      for name, item in value.items():
        if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9_]{1,40}', name):
          continue
        if any(word in name.split('_') for word in ('password', 'token', 'auth', 'key', 'secret', 'uid', 'id', 'session', 'device')):
          continue
        item = numeric_tree(item, depth + 1)
        if item is not None:
          public[name] = item
      return public
    if isinstance(value, list):
      return [item for original in value if (item := numeric_tree(original, depth + 1)) is not None]
    return None

  public = numeric_tree({name: obj[name] for name in ('protocol_version', 'data') if name in obj})
  return {'capability': dict(public or {}, cmd=obj['cmd'])}

def capture_material(path, *, config=None, decode_video=True):
  """Decode a closed pcap in RAM. Never print/serialize .credential or video bytes.

  tshark reads a single immutable in-memory snapshot. It runs offline with
  capture stdin; no packets are sent. The reference parser must exist locally.
  """
  cfg = config or load_config()
  path = Path(path)
  capture_bytes = path.read_bytes()
  credential = None

  def fields(filt, names):
    r = subprocess.run(['tshark', '-r', '-', '-Y', filt, '-T', 'fields', *[a for name in names for a in ('-e', name)]], input=capture_bytes, capture_output=True, check=True)
    return [x.split('\t') for x in r.stdout.decode().splitlines()]

  def chunks(segments):
    chunks = []
    cur = bytearray()
    end = None
    for seq, b in sorted(segments.items()):
      if end is None or seq > end:
        if cur:
          chunks.append(bytes(cur))
        cur = bytearray(b)
        end = seq + len(b)
      elif seq + len(b) > end:
        cur.extend(b[end - seq:])
        end = seq + len(b)
    if cur:
      chunks.append(bytes(cur))
    return chunks
  by = collections.defaultdict(dict)
  for st, src, dst, seq, pay in fields('tcp.port == 6668 && tcp.len > 0', ['tcp.stream', 'ip.src', 'ip.dst', 'tcp.seq_raw', 'tcp.payload']):
    by[st, src, dst][int(seq)] = bytes.fromhex(pay)
  tcp_coverage = []
  sessions = {}
  types_count = collections.Counter()
  bad = 0
  frames = 0
  commands = collections.Counter()
  shapes = collections.Counter()
  for (st, src, dst), segs in by.items():
    allchunks = chunks(segs)
    parsed_bytes = 0
    before_frames = frames
    for chunk in allchunks:
      pos = 0
      while pos + 16 <= len(chunk):
        if chunk[pos:pos + 4] != b'\x00\x00U\xaa':
          pos += 1
          continue
        magic, seq, cmd, size = struct.unpack_from('>IIII', chunk, pos)
        if not 8 <= size <= 65536 or pos + 16 + size > len(chunk):
          pos += 1
          continue
        frame = chunk[pos:pos + 16 + size]
        if frame[-4:] != b'\x00\x00\xaaU' or zlib.crc32(frame[:-8]) & 4294967295 != int.from_bytes(frame[-8:-4], 'big'):
          pos += 1
          continue
        pos += len(frame)
        frames += 1
        parsed_bytes += len(frame)
        commands[cmd] += 1
        if cmd not in (8, 9, 13, 16, 18, 32, 64):
          continue
        body = frame[16:-8]
        if src == cfg.ip:
          body = body[4:]
        if body.startswith(b'3.3'):
          body = body[15:]
        try:
          obj = json.loads(AESCipher(cfg.local_key.encode()).decrypt(body, False, decode_text=False, verify_padding=True))
        except Exception:
          bad += 1
          continue
        shapes[cmd, tuple(sorted(obj)) if isinstance(obj, dict) else type(obj).__name__] += 1
        head = obj.get('header', {})
        msg = obj.get('msg', {})
        kind = head.get('type')
        types_count[kind] += 1
        sid = head.get('sessionid')
        if not sid:
          continue
        session = sessions.setdefault((st, sid), {'phone': dst if src == cfg.ip else src, 'keys': {}, 'signals': collections.Counter(), 'sdp': {}, 'candidates': []})
        session['signals'][kind] += 1
        if isinstance(msg.get('candidate'), str):
          session['candidates'].append(msg['candidate'])
        if isinstance(msg.get('sdp'), str):
          sdp = msg['sdp']
          session['sdp'][kind] = sdp
          for line in sdp.splitlines():
            if line.startswith('a=aes-key:'):
              try:
                session['keys'][kind] = bytes.fromhex(line[10:])
              except ValueError:
                pass
    tcp_coverage.append({'stream': int(st), 'direction': 'camera_to_peer' if src == cfg.ip else 'peer_to_camera', 'chunks': len(allchunks), 'captured_bytes': sum(map(len, allchunks)), 'parsed_bytes': parsed_bytes, 'frames': frames - before_frames})
  lan_summary = {'frames': frames, 'tcp_coverage': tcp_coverage, 'commands': commands, 'shapes': [(str(k), v) for k, v in shapes.items()], 'invalid_decode': bad, 'signal_types': types_count, 'sessions': [{'signals': s['signals'], 'keys': {k: len(v) for k, v in s['keys'].items()}, 'media': [line for sdp in s['sdp'].values() for line in sdp.splitlines() if line.startswith(('m=', 'a=rtpmap:'))], 'candidate_count': len(s['candidates'])} for s in sessions.values()]}
  crypto = reference_crypto()
  from . import imm_kcp_segment as kcp
  udpstats = collections.Counter()
  groups = collections.defaultdict(dict)
  seen = set()
  keylabels = collections.Counter()
  for t, src, dst, sp, dp, pay in fields('udp && ip.addr == ' + cfg.ip, ['frame.time_relative', 'ip.src', 'ip.dst', 'udp.srcport', 'udp.dstport', 'udp.payload']):
    try:
      b = bytes.fromhex(pay)
    except ValueError:
      continue
    tup = (src, dst, sp, dp, b)
    if tup in seen:
      continue
    seen.add(tup)
    if len(b) >= 20 and b[4:8] == b'!\x12\xa4B':
      udpstats['stun'] += 1
      continue
    if len(b) < 44 or b[4] not in (81, 82, 83, 84):
      udpstats['not_kcp'] += 1
      continue
    udpstats['kcp_candidate'] += 1
    matched = None
    for si, session in enumerate(sessions.values()):
      if session['phone'] not in (src, dst):
        continue
      for keylabel, key in session['keys'].items():
        if len(key) == 16 and hmac.compare_digest(hmac.new(key, b[:-20], hashlib.sha1).digest(), b[-20:]):
          matched = (si, session, keylabel)
          break
      if matched:
        break
    if matched is None:
      udpstats['hmac_no_match'] += 1
      continue
    udpstats['hmac_verified'] += 1
    si, session, keylabel = matched
    keylabels['camera_to_phone' if src == cfg.ip else 'phone_to_camera', keylabel] += 1
    body = b[:-20]
    pos = 0
    while pos + 24 <= len(body):
      seg = kcp.parse_segment(body[pos:])
      if seg is None:
        break
      pos += 24 + len(seg.data)
      if seg.command != 81:
        continue
      role = 'camera_to_phone' if src == cfg.ip else 'phone_to_camera'
      group = (si, role, sp, dp, seg.conversation)
      if seg.sequence in groups[group]:
        udpstats['duplicate_kcp_push'] += 1
      else:
        groups[group][seg.sequence] = (seg, float(t))
    if pos != len(body):
      udpstats['kcp_trailing_bytes'] += 1
  records = collections.Counter()
  ctrls = []
  auth = {'client_login_present': False, 'admin_username': False, 'hex32_credential_present': False}
  mediaheaders = collections.Counter()
  unknownsizes = collections.Counter()
  videopayloads = []
  audio_stats = {'records': 0, 'bytes': 0, 'samples': 0, 'nonzero_samples': 0, 'peak': 0}
  audio_squares = 0
  audio_descriptions = collections.Counter()
  audio_lengths = collections.Counter()
  audio_times = []
  for (si, role, sp, dp, conv), segs in groups.items():
    pending = []
    last = None
    expect = None
    keys = sessions[list(sessions)[si]]['keys']
    order = ['answer', 'offer'] if role == 'camera_to_phone' else ['offer', 'answer']
    for sn, (seg, t) in sorted(segs.items()):
      if last is not None and (sn != last + 1 or seg.fragment != expect):
        pending = []
        records['fragment_resets'] += 1
      pending.append(seg.data)
      last = sn
      expect = seg.fragment - 1
      if seg.fragment:
        continue
      record = b''.join(pending)
      pending = []
      last = None
      expect = None
      records['complete_kcp_records'] += 1
      plain = None
      used = None
      for keyname in order:
        key = keys.get(keyname)
        if not key or len(key) != 16:
          continue
        try:
          p = crypto.decrypt_record(key, record)
        except Exception:
          continue
        if plain is None:
          plain = p
          used = keyname
        if p[:4] == b'xV4\x12' or (len(p) >= 40 and int.from_bytes(p[:4], 'little') in (65539, 65541)):
          plain = p
          used = keyname
          break
      if plain is None:
        records['cbc_rejected'] += 1
        continue
      records['cbc_' + role + '_' + used] += 1
      if plain[:4] == b'xV4\x12':
        records['control_records'] += 1
        req = int.from_bytes(plain[4:8], 'little')
        if role == 'phone_to_camera' and req == 1 and (len(plain) >= 104):
          auth['client_login_present'] = True
          auth['admin_username'] = plain[8:40].rstrip(b'\x00') == b'admin'
          token = plain[40:72].rstrip(b'\x00')
          auth['hex32_credential_present'] = len(token) == 32 and all((c in b'0123456789abcdefABCDEF' for c in token))
          credential = token.decode('ascii') if auth['hex32_credential_present'] else credential
          continue
        if len(plain) >= 20:
          magic, request, zero, cmd, sub, n = struct.unpack_from('<IIIHHI', plain)
          row = {'time': round(t, 3), 'direction': role, 'command': cmd, 'sub': sub, 'payload_bytes': n, 'payload_complete': len(plain) >= 20 + n}
          if cmd == 9 and n == 8 and (len(plain) >= 28):
            channel, clarity = struct.unpack_from('<II', plain, 20)
            if channel <= 16 and clarity in (0, 1, 2, 3, 4, 8, 65535):
              row.update(channel=channel, clarity=clarity)
          if len(plain) >= 20 + n:
            row.update(summarize_control_payload(cmd, sub, plain[20:20 + n]))
          ctrls.append(row)
      elif len(plain) >= 40 and int.from_bytes(plain[:4], 'little') in (65539, 65541):
        cmd = int.from_bytes(plain[:4], 'little')
        records['video_records' if cmd == 65539 else 'audio_records'] += 1
        if cmd == 65539:
          w, h = struct.unpack_from('<HH', plain, 26)
          mediaheaders[w, h] += 1
          sub = plain[32:]
          n = int.from_bytes(sub[:4], 'little')
          payload = sub[16:] if len(sub) >= 16 and n >= 12 and (n - 12 == len(sub) - 16) else sub[8:]
          videopayloads.append((t, payload, w, h))
        else:
          sub = plain[32:]
          n = int.from_bytes(sub[:4], 'little')
          payload = sub[16:] if len(sub) >= 16 and n >= 12 and n - 12 == len(sub) - 16 else sub[8:]
          audio_descriptions[plain[24], int.from_bytes(plain[26:28], 'little'), plain[30]] += 1
          audio_lengths[len(payload)] += 1
          audio_times.append(t)
          audio_stats['records'] += 1
          audio_stats['bytes'] += len(payload)
          values = struct.unpack('<' + 'h' * (len(payload) // 2), payload[:len(payload) // 2 * 2])
          audio_stats['samples'] += len(values)
          audio_stats['nonzero_samples'] += sum(value != 0 for value in values)
          audio_stats['peak'] = max(audio_stats['peak'], max(map(abs, values), default=0))
          audio_squares += sum(value * value for value in values)
      else:
        records['unknown_plaintext'] += 1
        unknownsizes[len(plain)] += 1
  audio_stats['rms'] = round(math.sqrt(audio_squares / max(1, audio_stats['samples'])), 3)
  audio_stats['capture_elapsed_seconds'] = round(max(audio_times) - min(audio_times), 3) if audio_times else 0
  audio_stats['payload_lengths'] = dict(audio_lengths)
  audio_stats['descriptions'] = [{'codec': codec, 'rate_index': rate, 'channels': channels, 'records': count}
                                 for (codec, rate, channels), count in audio_descriptions.items()]
  media_summary = {'udp': udpstats, 'mac_key_origin_counts': [(list(k), v) for k, v in keylabels.items()], 'records': records, 'auth': auth, 'controls': ctrls, 'video_header_sizes': [{'width': w, 'height': h, 'records': n} for (w, h), n in mediaheaders.items()], 'unknown_plaintext_sizes': unknownsizes,
                   'audio_pcm_s16le_hypothesis': audio_stats}
  nal_summary = {'first_nal_types': dict(collections.Counter((p[0] & 31 for t, p, w, h in videopayloads if p))), 'annexb_payloads': sum((p.startswith((bytes([0, 0, 0, 1]), bytes([0, 0, 1]))) for t, p, w, h in videopayloads)), 'payload_lengths_head': dict(collections.Counter((len(p) for t, p, w, h in videopayloads)).most_common(12))}
  import av
  annexb = []
  fua = None
  nalstats = collections.Counter()
  sps_times = []
  for t, p, w, h in videopayloads:
    if not p:
      continue
    typ = p[0] & 31
    if 1 <= typ <= 23:
      annexb.append(b'\x00\x00\x00\x01' + p)
      nalstats[typ] += 1
      if typ == 7:
        sps_times.append((t, p))
    elif typ == 28 and len(p) >= 2:
      if p[1] & 128:
        fua = bytearray(bytes([p[0] & 224 | p[1] & 31]) + p[2:])
      elif fua is not None:
        fua.extend(p[2:])
      if p[1] & 64 and fua is not None:
        annexb.append(b'\x00\x00\x00\x01' + fua)
        nalstats[fua[0] & 31] += 1
        fua = None
  decoded_sizes = collections.Counter()
  decode_errors = 0
  if decode_video:
    ctx = av.CodecContext.create('h264', 'r')
    decoded_sizes = collections.Counter()
    decode_errors = 0
    for n in annexb:
      try:
        for packet in ctx.parse(n):
          for f in ctx.decode(packet):
            decoded_sizes[f.width, f.height] += 1
      except Exception:
        decode_errors += 1
    try:
      for packet in ctx.parse(b''):
        for f in ctx.decode(packet):
          decoded_sizes[f.width, f.height] += 1
      for f in ctx.decode(None):
        decoded_sizes[f.width, f.height] += 1
    except Exception:
      decode_errors += 1
  decoded_summary = {'frame_sizes': [{'width': w, 'height': h, 'frames': n} for (w, h), n in decoded_sizes.items()], 'decode_errors': decode_errors, 'nal_types': dict(nalstats)}
  if credential is None:
    raise CaptureDecodeError('No authenticated client login credential found')
  summary = {'capture': path.name, 'sha256': hashlib.sha256(capture_bytes).hexdigest(), 'lan': lan_summary, 'media': media_summary, 'nal': nal_summary, 'decoded_video': decoded_summary}
  return CaptureMaterial(credential, summary, b''.join(annexb))

def write_hd_evidence(material, directory):
  """Write only actual decoded 1280x720 frames, silent video, mode 0600.

  Existing files are never replaced. Video is re-encoded as H264 at 25 fps;
  dimensions are preserved exactly and audio is deliberately absent.
  """
  import av
  import os
  from fractions import Fraction
  directory = Path(directory)
  directory.mkdir(mode=448, parents=True, exist_ok=True)
  decoder = av.CodecContext.create('h264', 'r')
  frames = []
  packets = decoder.parse(material.video_annexb) + decoder.parse(b'')
  for packet in packets:
    for frame in decoder.decode(packet):
      if (frame.width, frame.height) == (1280, 720):
        frames.append(frame)
  for frame in decoder.decode(None):
    if (frame.width, frame.height) == (1280, 720):
      frames.append(frame)
  if not frames:
    raise CaptureDecodeError('No actual 1280x720 frames available')
  photo = directory / 'hd-first-frame.jpg'
  video = directory / 'hd-short-proof.mp4'
  created = []
  try:
    fd = os.open(photo, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 384)
    created.append(photo)
    with os.fdopen(fd, 'wb') as output:
      frames[0].to_image().save(output, format='JPEG', quality=95)
    fd = os.open(video, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 384)
    created.append(video)
    with os.fdopen(fd, 'wb') as output:
      with av.open(output, 'w', format='mp4') as container:
        stream = container.add_stream('libx264', rate=25)
        stream.width, stream.height, stream.pix_fmt = (1280, 720, 'yuv420p')
        for i, frame in enumerate(frames):
          assert (frame.width, frame.height) == (1280, 720)
          frame.pts, frame.time_base = (i, Fraction(1, 25))
          for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
          container.mux(packet)
  except BaseException:
    for path in created:
      path.unlink(missing_ok=True)
    raise
  return {'photo': str(photo), 'video': str(video), 'width': 1280, 'height': 720, 'frames': len(frames), 'fps': 25, 'duration': len(frames) / 25, 'audio': False, 'reencoded': True, 'resized': False}
