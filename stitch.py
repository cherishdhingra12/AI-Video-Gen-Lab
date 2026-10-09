"""
stitch.py — join downloaded clips into one video. Local ffmpeg, no cloud.

Extracted from the old modal_gpu.render_project. Two differences from that:
  - system ffmpeg/ffprobe (clips come from websites now, not a Modal image)
  - real per-clip durations via ffprobe (sites return varying lengths), so the
    xfade offsets are correct instead of assuming a fixed frame count.

Exports carry the ORIGINAL audio: each clip's own sound is cross-faded in step with
its video (acrossfade mirrors the video xfade, so audio and picture stay in sync and
the totals match). A clip with no audio stream gets silence of its own duration so the
timeline stays aligned; if NO clip has audio the output stays video-only (the gate then
says so) — silence is never invented as if it were sound.

    stitch(["a.mp4","b.mp4"], "out.mp4", xfade=0.4)         # crossfade + original audio
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


def _has_audio(path: str) -> bool:
    """True when the file actually carries an audio stream (checked, not assumed)."""
    res = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=index", "-of", "csv=p=0", path],
        capture_output=True, text=True,
    )
    return bool(res.stdout.strip())


def stitch(clips: list[str], out: str, xfade: float = 0.4, fps: int = 24,
           w: int = 1080, h: int = 1920) -> str:
    """Join clips into `out`, each normalized to a w×h 9:16 canvas so clips from
    different sites (each a different pixel size) join cleanly — xfade/concat both
    require identical dimensions. xfade>0 crossfades neighbours; else hard-cut concat.
    The clips' own audio is carried through (see module docstring) and re-encoded to AAC."""
    if not clips:
        raise ValueError("no clips to stitch")

    # per-input normalize: fit inside w×h, pad to fill, square pixels, fixed fps
    def norm(i):
        return (f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}[v{i}]")

    inputs = []
    for c in clips:
        inputs += ["-i", c]
    has = [_has_audio(c) for c in clips]
    audio = any(has)                       # no source sound at all -> stay video-only

    if len(clips) == 1:
        maps = ["-map", "[v0]"]
        if has[0]:
            maps += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "192k"]
        subprocess.run([FFMPEG, "-y", *inputs, "-filter_complex", norm(0), *maps,
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", out],
                       check=True)
        return out

    durs = [_dur(c) for c in clips]

    # silence stand-ins for clips WITHOUT audio, so the audio timeline still lines up
    # with the picture (a gap would drag every later clip's sound out of sync)
    silence = {}
    if audio and not all(has):
        for k, ok in enumerate(has):
            if not ok:
                silence[k] = len(clips) + len(silence)
                inputs += ["-f", "lavfi", "-t", f"{durs[k]:.3f}", "-i",
                           "anullsrc=channel_layout=stereo:sample_rate=48000"]

    fc = [norm(i) for i in range(len(clips))]
    alab = []
    if audio:
        for k in range(len(clips)):        # one common format — acrossfade/concat require it
            src = k if has[k] else silence[k]
            fc.append(f"[{src}:a:0]aformat=sample_rates=48000:channel_layouts=stereo[a{k}]")
            alab.append(f"a{k}")

    # a crossfade wider than the shortest clip would eat a whole scene -> hard concat
    if xfade <= 0 or xfade >= min(durs):
        fc.append("".join(f"[v{i}]" for i in range(len(clips)))
                  + f"concat=n={len(clips)}:v=1:a=0[out]")
        maps = ["-map", "[out]"]
        if alab:
            fc.append("".join(f"[{l}]" for l in alab)
                      + f"concat=n={len(alab)}:v=0:a=1[outa]")
            maps += ["-map", "[outa]", "-c:a", "aac", "-b:a", "192k"]
        subprocess.run([FFMPEG, "-y", *inputs, "-filter_complex", ";".join(fc),
                        *maps, "-c:v", "libx264", "-pix_fmt", "yuv420p", out],
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
    maps = ["-map", f"[{prev}]"]
    if alab:
        # mirror the video chain: acrossfade overlaps by the same xfade, so the audio
        # totals match the picture exactly (sum - (n-1)*xfade) and each clip's sound
        # sits on its own clip's frames
        ap = alab[0]
        for i in range(1, len(alab)):
            fc.append(f"[{ap}][{alab[i]}]acrossfade=d={xfade}[ac{i}]")
            ap = f"ac{i}"
        maps += ["-map", f"[{ap}]", "-c:a", "aac", "-b:a", "192k"]
    subprocess.run([FFMPEG, "-y", *inputs, "-filter_complex", ";".join(fc),
                    *maps, "-c:v", "libx264", "-pix_fmt", "yuv420p",
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
    # different sites), each WITH its own audio (44.1k + 22.05k — aformat must unify them),
    # stitched with xfade. Assert:
    #   (1) output duration = sum - overlap (video offset math) AND the audio is the same
    #       length — the export has to carry the clips' original sound, in step with picture,
    #   (2) output is normalized to 1080x1920 9:16 (the mixed-size fix),
    #   (3) clips with NO audio stay video-only — silence is never invented as if it were sound.
    import json as _json, os, tempfile
    d = tempfile.mkdtemp()
    a, b, o = f"{d}/a.mp4", f"{d}/b.mp4", f"{d}/o.mp4"
    for path, color, size, sr in ((a, "red", "480x854", 44100),
                                  (b, "blue", "720x1280", 22050)):
        subprocess.run([FFMPEG, "-y", "-f", "lavfi", "-i",
                        f"color=c={color}:s={size}:d=2:r=24",
                        "-f", "lavfi", "-i", f"sine=frequency=440:d=2:sample_rate={sr}",
                        "-shortest", "-pix_fmt", "yuv420p", path],
                       check=True, capture_output=True)
    stitch([a, b], o, xfade=0.4, fps=24)
    got = _dur(o)
    want = 2 + 2 - 0.4
    assert abs(got - want) < 0.25, f"xfade duration off: got {got:.2f}, want ~{want:.2f}"
    dim = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                          "-show_entries", "stream=width,height", "-of", "json", o],
                         capture_output=True, text=True, check=True).stdout
    s = _json.loads(dim)["streams"][0]
    assert (s["width"], s["height"]) == (1080, 1920), f"not normalized to 9:16: {s}"
    assert _has_audio(o), "the export must carry the clips' original audio"
    au = _json.loads(subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream=sample_rate,channels,duration", "-of", "json", o],
        capture_output=True, text=True, check=True).stdout)["streams"][0]
    assert au["sample_rate"] == "48000" and int(au["channels"]) == 2, f"audio not unified: {au}"
    assert abs(float(au["duration"]) - want) < 0.35, \
        f"audio must stay in step with the picture: audio {au['duration']}s vs video {got:.2f}s"
    # no source sound anywhere -> video-only (so the gate can still report its absence)
    s1, s2, so = f"{d}/s1.mp4", f"{d}/s2.mp4", f"{d}/so.mp4"
    for path, color in ((s1, "green"), (s2, "yellow")):
        subprocess.run([FFMPEG, "-y", "-f", "lavfi", "-i",
                        f"color=c={color}:s=320x568:d=2:r=24", "-pix_fmt", "yuv420p", path],
                       check=True, capture_output=True)
    stitch([s1, s2], so, xfade=0.4)
    assert not _has_audio(so), "no source audio anywhere -> stay video-only, invent nothing"
    # MIXED: one clip with sound, one without — the silent clip must be filled with silence of
    # its own length so the real sound stays in step (this is the case that broke an export)
    sm = f"{d}/sm.mp4"
    stitch([a, s1], sm, xfade=0.4)
    assert _has_audio(sm), "mixed sources must still export with the audio that exists"
    smd = _dur(sm)
    assert abs(smd - want) < 0.25, f"mixed stitch duration off: {smd:.2f} vs {want:.2f}"
    last_frame(a, f"{d}/last.png")
    assert os.path.getsize(f"{d}/last.png") > 0, "last_frame produced nothing"
    print(f"OK: stitched 2 mixed-size clips -> {got:.2f}s (want ~{want:.2f}), 9:16 + "
          f"original audio {au['duration']}s @48k stereo (in step); video-only and "
          f"mixed-silence cases covered; last_frame OK")
