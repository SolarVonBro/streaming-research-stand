"""Видеоконтент: размеры сегментов из артефакта Pensieve, содержимое — детерминированные байты.

Алгоритмам адаптации важен только размер сегмента, поэтому сами байты не декодируются.
Тело сегмента — срез одного буфера, заполненного генератором с фиксированным сидом,
так что ответ на один и тот же URL всегда побайтно одинаков.
"""
from __future__ import annotations

import csv
import random
from dataclasses import dataclass

from . import settings


@dataclass(frozen=True)
class Video:
    video_id: str
    bitrates_kbps: list[int]
    sizes: dict[tuple[int, int], int]  # (level, segment) -> bytes
    segments: int
    segment_seconds: float


def load_video() -> Video:
    sizes: dict[tuple[int, int], int] = {}
    bitrates: dict[int, int] = {}
    with open(settings.MEDIA_SIZES, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            level, seg = int(row["level"]), int(row["segment"])
            sizes[(level, seg)] = int(row["bytes"])
            bitrates[level] = int(row["bitrate_kbps"])
    segments = max(seg for _, seg in sizes)
    return Video(settings.VIDEO_ID, [bitrates[k] for k in sorted(bitrates)], sizes, segments,
                 settings.SEGMENT_SECONDS)


VIDEO = load_video()
_PAYLOAD = random.Random(20261118).randbytes(max(VIDEO.sizes.values()))


def segment_bytes(level: int, segment: int) -> bytes | None:
    size = VIDEO.sizes.get((level, segment))
    return None if size is None else _PAYLOAD[:size]


def build_mpd(base: str = "/media") -> str:
    dur = VIDEO.segments * VIDEO.segment_seconds
    reps = "\n".join(
        f'      <Representation id="{i}" bandwidth="{kbps * 1000}" codecs="avc1.4D401E"/>'
        for i, kbps in enumerate(VIDEO.bitrates_kbps)
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" profiles="urn:mpeg:dash:profile:isoff-live:2011"
     minBufferTime="PT4S" mediaPresentationDuration="PT{dur:.0f}S">
  <Period id="0">
    <AdaptationSet mimeType="video/mp4" segmentAlignment="true">
      <SegmentTemplate timescale="1" duration="{VIDEO.segment_seconds:g}" startNumber="1"
                       media="{base}/{VIDEO.video_id}/$RepresentationID$/$Number$.m4s"/>
{reps}
    </AdaptationSet>
  </Period>
</MPD>
"""
