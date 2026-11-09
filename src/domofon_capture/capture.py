"""Atomic JPEG and silent H264 MP4 capture from decoded video frames."""
import asyncio
import math
import os
import tempfile
from fractions import Fraction
from pathlib import Path
import av

class CaptureError(RuntimeError):
  pass

SIZES = {'sd':(640,360), 'hd':(1280,720)}

def positive_finite(value):
  value = float(value)
  if not math.isfinite(value) or value <= 0:
    raise ValueError('Value must be positive and finite')
  return value

async def capture(frames, output, kind, resolution, duration=10, overwrite=False, timeout=30, before_publish=None):
  duration = positive_finite(duration)
  output = Path(output)
  if not overwrite and output.exists():
    raise FileExistsError('Output already exists; use --overwrite')
  fd, temp_name = tempfile.mkstemp(prefix='.' + output.name + '.', suffix='.tmp', dir=output.parent)
  os.close(fd)
  temp = Path(temp_name)
  container = None
  audio_stream = None
  resampler = None
  audio_pts = 0
  matched = False
  elapsed = 0
  first_time = previous_time = None
  deadline = asyncio.get_running_loop().time() + timeout
  try:
    async for frame in frames:
      if isinstance(frame, av.AudioFrame):
        if container is not None and audio_stream is not None:
          for out in resampler.resample(frame):
            out.pts = audio_pts
            out.time_base = Fraction(1,48000)
            audio_pts += out.samples
            for packet in audio_stream.encode(out): container.mux(packet)
        continue
      if (frame.width,frame.height) != SIZES[resolution]:
        if matched or asyncio.get_running_loop().time() >= deadline:
          raise CaptureError('Unsupported resolution: camera did not supply requested dimensions')
        continue
      matched = True
      if kind == 'photo':
        frame.to_image().save(temp, format='JPEG', quality=95)
        break
      if frame.pts is None or frame.time_base is None:
        raise CaptureError('Camera video lacks media timestamps')
      timestamp = Fraction(frame.pts) * frame.time_base
      if first_time is None:
        first_time = timestamp
        container = av.open(str(temp), mode='w', format='mp4')
        audio_stream = container.add_stream('aac', rate=48000)
        audio_stream.layout = 'mono'
        resampler = av.AudioResampler(format='fltp', layout='mono', rate=48000)
        stream = container.add_stream('libx264', rate=25)
        stream.width, stream.height = SIZES[resolution]
        stream.pix_fmt = 'yuv420p'
        stream.time_base = Fraction(1,90000)
        stream.codec_context.time_base = Fraction(1,90000)
        stream.options = {'preset':'ultrafast', 'crf':'23'}
      if previous_time is not None and timestamp <= previous_time:
        raise CaptureError('Camera video timestamps did not advance')
      previous_time = timestamp
      elapsed = float(timestamp-first_time)
      frame = frame.reformat(format='yuv420p')
      frame.pts = round(min(elapsed, duration)*90000)
      frame.time_base = Fraction(1,90000)
      for packet in stream.encode(frame):
        container.mux(packet)
      if elapsed >= duration:
        for packet in stream.encode():
          container.mux(packet)
        if audio_stream is not None:
          for packet in audio_stream.encode(): container.mux(packet)
        container.close()
        container = None
        break
    else:
      raise CaptureError('Camera ended before requested capture completed')
    if not matched:
      raise CaptureError('Unsupported resolution: camera did not supply requested dimensions')
    if container is not None:
      raise CaptureError('Video ended before requested duration')
    if temp.stat().st_size == 0:
      raise CaptureError('Capture produced an empty file')
    if kind == 'video':
      with av.open(str(temp)) as check:
        video = check.streams.video[0]
        actual_duration = float(check.duration / av.time_base) if check.duration else 0
        if video.codec_context.name != 'h264' or (video.width,video.height) != SIZES[resolution] or actual_duration + .1 < duration:
          raise CaptureError('Recorded video failed duration or codec validation')
    else:
      actual_duration = None
    if before_publish is not None:
      await before_publish()
    with temp.open('rb') as f:
      os.fsync(f.fileno())
    if overwrite:
      os.replace(temp, output)
    else:
      os.link(temp, output)
      temp.unlink()
    return {'output':str(output), 'width':SIZES[resolution][0], 'height':SIZES[resolution][1], **({'duration':actual_duration} if kind=='video' else {})}
  finally:
    try:
      if container:
        container.close()
    finally:
      temp.unlink(missing_ok=True)
