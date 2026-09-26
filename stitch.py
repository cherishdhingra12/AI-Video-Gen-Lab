"""
stitch.py — join downloaded clips into one video. Local ffmpeg, no cloud.

Extracted from the old modal_gpu.render_project. Two differences from that:
  - system ffmpeg/ffprobe (clips come from websites now, not a Modal image)
  - real per-clip durations via ffprobe (sites return varying lengths), so the
    xfade offsets are correct instead of assuming a fixed frame count.

    stitch(["a.mp4","b.mp4"], "out.mp4", xfade=0.4)         # crossfade
    last_frame("a.mp4", "a_last.png")                        # for image->video chaining
"""

import json
import subprocess

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"


def _dur(path: str) -> float:
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "json", path],
        capture_output=True, text=True, check=True,
    ).stdout
    return float(json.loads(out)["format"]["duration"])


def stitch(clips: list[str], out: str, xfade: float = 0.4, fps: int = 24,
           w: int = 1080, h: int = 1920) -> str:
    """Join clips into `out`, each normalized to a w×h 9:16 canvas so clips from
    different sites (each a different pixel size) join cleanly — xfade/concat both
    require identical dimensions. xfade>0 crossfades neighbours; else hard-cut concat.
    ponytail: video-only (drops per-clip audio); add amix/concat-audio if a voiced reel is needed."""
    if not clips:
        raise ValueError("no clips to stitch")

    # per-input normalize: fit inside w×h, pad to fill, square pixels, fixed fps
    def norm(i):
        return (f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}[v{i}]")

    inputs = []
    for c in clips:
        inputs += ["-i", c]

    if len(clips) == 1:
        subprocess.run([FFMPEG, "-y", *inputs, "-filter_complex", norm(0),
                        "-map", "[v0]", "-c:v", "libx264", "-pix_fmt", "yuv420p", out],
                       check=True)
        return out

    durs = [_dur(c) for c in clips]
    fc = [norm(i) for i in range(len(clips))]

    # a crossfade wider than the shortest clip would eat a whole scene -> hard concat
    if xfade <= 0 or xfade >= min(durs):
        fc.append("".join(f"[v{i}]" for i in range(len(clips)))
                  + f"concat=n={len(clips)}:v=1:a=0[out]")
        subprocess.run([FFMPEG, "-y", *inputs, "-filter_complex", ";".join(fc),
                        "-map", "[out]", "-c:v", "libx264", "-pix_fmt", "yuv420p", out],
                       check=True)
        return out

    # chain xfades over the normalized streams: each transition starts xfade secs before the running end.
    prev, off = "v0", 0.0
    for i in range(1, len(clips)):
        off += durs[i - 1] - xfade          # running end of prev clip minus the overlap
        label = f"x{i}"
        fc.append(f"[{prev}][v{i}]xfade=transition=fade:"
                  f"duration={xfade}:offset={off:.3f}[{label}]")
        prev = label
    subprocess.run([FFMPEG, "-y", *inputs, "-filter_complex", ";".join(fc),
                    "-map", f"[{prev}]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-r", str(fps), out], check=True)
    return out


def last_frame(clip: str, out_png: str) -> str:
    """Grab the final frame — feeds the next clip's first frame on image->video
    sites, so consecutive clips share a boundary (the keyframe-chain idea)."""
    subprocess.run([FFMPEG, "-y", "-sseof", "-0.1", "-i", clip,
                    "-update", "1", "-q:v", "2", out_png], check=True)
    return out_png


if __name__ == "__main__":
    # self-check: two DIFFERENT-SIZE clips (the real waterfall case — clips come from
    # different sites), stitch with xfade, assert (1) output duration = sum - overlap
    # (offset math), and (2) output is normalized to 1080x1920 9:16 (the mixed-size fix).
    import json as _json, os, tempfile
    d = tempfile.mkdtemp()
    a, b, o = f"{d}/a.mp4", f"{d}/b.mp4", f"{d}/o.mp4"
    for path, color, size in ((a, "red", "480x854"), (b, "blue", "720x1280")):
        subprocess.run([FFMPEG, "-y", "-f", "lavfi", "-i",
                        f"color=c={color}:s={size}:d=2:r=24", "-pix_fmt", "yuv420p",
                        path], check=True, capture_output=True)
    stitch([a, b], o, xfade=0.4, fps=24)
    got = _dur(o)
    want = 2 + 2 - 0.4
    assert abs(got - want) < 0.25, f"xfade duration off: got {got:.2f}, want ~{want:.2f}"
    dim = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                          "-show_entries", "stream=width,height", "-of", "json", o],
                         capture_output=True, text=True, check=True).stdout
    s = _json.loads(dim)["streams"][0]
    assert (s["width"], s["height"]) == (1080, 1920), f"not normalized to 9:16: {s}"
    last_frame(a, f"{d}/last.png")
    assert os.path.getsize(f"{d}/last.png") > 0, "last_frame produced nothing"
    print(f"OK: stitched 2 mixed-size clips -> {got:.2f}s (want ~{want:.2f}), "
          f"normalized to {s['width']}x{s['height']} 9:16; last_frame OK")
