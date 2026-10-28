"""영상·음성 어댑터.

- FFmpegSlideshow: 승인된 스토리의 실제 이미지(PNG/JPG)를 장면 길이에 맞춰 이어 붙여 세로 영상(MP4)을 만듭니다.
  FFmpeg 이 설치되어 있어야 합니다.
- 음성(TTS)은 아직 어댑터가 연결되지 않았습니다. 호출하면 NOT_CONFIGURED 를 그대로 반환하며,
  지원되는 기능인 것처럼 꾸미지 않습니다.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class MediaNotConfigured(Exception):
    pass


class FFmpegSlideshow:
    name = "ffmpeg-slideshow"

    def __init__(self, ffmpeg_path: str = "ffmpeg"):
        self.ffmpeg = shutil.which(ffmpeg_path) or (ffmpeg_path if Path(ffmpeg_path).exists() else None)

    def available(self) -> bool:
        return self.ffmpeg is not None

    def render(self, images: list[Path], durations: list[float], out_path: Path, width=1080, height=1920) -> dict:
        if not self.ffmpeg:
            raise MediaNotConfigured("FFmpeg 이 설치되어 있지 않습니다. AIS_FFMPEG 에 ffmpeg.exe 경로를 지정하세요.")
        if not images:
            raise MediaNotConfigured("영상으로 만들 이미지가 없습니다. 먼저 이미지를 생성하세요.")
        listfile = out_path.with_suffix(".txt")
        lines = []
        for img, dur in zip(images, durations):
            lines.append(f"file '{img.as_posix()}'")
            lines.append(f"duration {dur}")
        lines.append(f"file '{images[-1].as_posix()}'")
        listfile.write_text("\n".join(lines), encoding="utf-8")
        vf = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
              f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,format=yuv420p")
        cmd = [self.ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(listfile), "-vf", vf, "-r", "30",
               "-c:v", "libx264", str(out_path)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            raise RuntimeError(f"FFmpeg 실패: {proc.stderr[-500:]}")
        return {"model": self.name, "width": width, "height": height}


class NotConfiguredAudio:
    name = "none"

    def available(self) -> bool:
        return False

    def synthesize(self, text: str, voice: dict, out_path: Path) -> dict:
        raise MediaNotConfigured(
            "음성 생성(TTS) 어댑터가 연결되어 있지 않습니다. 로컬 TTS 또는 외부 음성 API 어댑터를 추가해야 합니다."
        )
