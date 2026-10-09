"""music.py — find, measure, choose and archive the score for a reel.

The library at music/ already had a hand-run version of this: measure each track's energy above
4 kHz, compare, pick by fit ("a solemn bansuri under a bunny-ear joke reads as the wrong film").
This automates that loop, and adds the half that did not exist — finding a track that is not in
the library yet.

  profile(...)            -> what a reel needs (mood, minimum length, brightness)
  library()               -> every archived track with its measured profile
  pick(profile)           -> the best FIT in the library, or None
  find(profile)           -> search, download candidates, measure them, keep the best one
  install(project_dir, t) -> copy the chosen track in as music_raw.mp3 (what the assembler reads)

Fit is MEASURED, never guessed from a filename — exactly the practice LIBRARY.md records.

Provenance is recorded on every ingest but never gates a choice (owner instruction, 2026-10-06,
and restated 2026-10-08): quality first, source logged in LIBRARY.md so it is never lost.

    python3 music.py --selfcheck
    python3 music.py --find "mournful evening bansuri"     # search + add to the library
"""
import json
import os
import re
import shutil
import subprocess
import sys

import numpy as np

_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_DIR)
LIB = os.path.join(ROOT, "music")
INDEX = os.path.join(LIB, ".analysis.json")
RATE = 22050

# Where "brightness" should land for each mood, as a rank within the library (0 dim .. 1 bright).
# The measured signal is energy above 4 kHz — percussive, folk and playful material sits high;
# a solo bansuri at dusk sits low. LIBRARY.md reached the same conclusion by hand.
WANT_RANK = {"bright": 0.92, "playful": 0.80, "warm": 0.66, "neutral": 0.50,
             "tender": 0.35, "mournful": 0.16, "sombre": 0.06}
FIT_MIN = 0.72
MAX_TRACK = 900.0            # past this it is a compilation, not a background bed


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def _seconds(path):
    r = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
              "-of", "json", path]).stdout
    try:
        return float(json.loads(r)["format"]["duration"])
    except Exception:  # noqa: BLE001
        return 0.0


def _mono(path, secs=None):
    cmd = ["ffmpeg", "-v", "error"]
    if secs:
        cmd += ["-t", f"{secs:.3f}"]
    cmd += ["-i", path, "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"]
    p = subprocess.run(cmd, capture_output=True)
    return np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def _hf_db(x):
    """Energy above 4 kHz, as a dB ratio to the whole band — the exact measure LIBRARY.md uses
    to tell a bright folk track from a dim evening raga."""
    if x.size < 4096:
        return -99.0
    win = np.hanning(4096)
    band, total = 0.0, 0.0
    for i in range(0, len(x) - 4096, 4096):
        s = np.abs(np.fft.rfft(x[i:i + 4096] * win)) ** 2
        freqs = np.fft.rfftfreq(4096, 1.0 / RATE)
        band += float(s[freqs >= 4000].sum())
        total += float(s.sum())
    if total <= 0:
        return -99.0
    return 10 * np.log10(max(band, 1e-12) / total)


def _onsets(x, hop=512):
    """Onset times from a half-wave-rectified energy flux — enough to ask whether beats land on
    the reel's cuts, which is what 'it syncs' means here."""
    n = len(x) // hop
    if n < 8:
        return np.array([]), 0.0
    frames = x[:n * hop].reshape(n, hop)
    env = np.sqrt((frames ** 2).mean(1) + 1e-12)
    flux = np.maximum(np.diff(env), 0)
    if flux.max() <= 0:
        return np.array([]), 0.0
    thresh = flux.mean() + 1.5 * flux.std()
    times = (np.nonzero(flux > thresh)[0] + 1) * hop / RATE
    # A steady tone is not a slow one. Its flux is only frame-boundary ripple, which is
    # scale-free enough to pass any threshold above, so a real onset must also be a real jump
    # in energy — otherwise a held bansuri reports a confident nonsense tempo.
    if times.size < 4 or flux.max() < 0.25 * env.mean():
        return np.array([]), 0.0
    # tempo from the autocorrelation of the flux: the lag that best explains its own rhythm
    f = flux - flux.mean()
    ac = np.correlate(f, f, "full")[len(f) - 1:]
    lo, hi = int(60 * RATE / hop / 180), int(60 * RATE / hop / 60)
    bpm = 0.0
    if hi > lo and ac[lo:hi].max() > 0:
        bpm = float(60 * RATE / hop / (lo + int(np.argmax(ac[lo:hi]))))
    return times, bpm


def analyse(path):
    """The measured profile of a track. Cached by (name, size, mtime) so a library scan is
    cheap after the first pass."""
    try:
        st = os.stat(path)
        key = f"{os.path.basename(path)}:{st.st_size}:{int(st.st_mtime)}"
        cache = json.load(open(INDEX)) if os.path.exists(INDEX) else {}
    except OSError:
        key, cache = None, {}
    if key and key in cache:
        return cache[key]

    x = _mono(path, secs=90.0)                    # 90s is plenty to characterise a bed
    onsets, bpm = _onsets(x)
    m = {"file": os.path.basename(path), "path": path, "seconds": round(_seconds(path), 2),
         "rms_db": round(float(20 * np.log10(max(float(np.sqrt((x ** 2).mean())), 1e-9))), 2)
         if x.size else -99.0,
         "hf_db": round(float(_hf_db(x)), 2),
         "bpm": round(bpm, 1), "onsets": [round(float(t), 3) for t in onsets[:400]]}
    if key:
        cache[key] = m
        os.makedirs(LIB, exist_ok=True)
        with open(INDEX, "w") as f:
            json.dump(cache, f, indent=1)
    return m


def library():
    if not os.path.isdir(LIB):
        return []
    return [analyse(os.path.join(LIB, f)) for f in sorted(os.listdir(LIB))
            if f.lower().endswith((".mp3", ".m4a", ".wav", ".aac", ".opus"))]


def _ranks(tracks):
    """Brightness rank per track within the library — fit is relative, not an absolute dB, so
    the judgement holds however the shelf is stocked."""
    order = sorted(range(len(tracks)), key=lambda i: tracks[i]["hf_db"])
    return {i: (r / max(1, len(tracks) - 1)) for r, i in enumerate(order)}


def fit(track, profile, rank=0.5):
    """0..1 — how well a measured track suits a profile. None when it is simply too short."""
    need = profile.get("min_seconds", 0)
    if track["seconds"] < need:
        return None
    want = profile.get("want", "any")
    if want == "any":
        return 0.8
    return round(1.0 - abs(rank - WANT_RANK.get(want, 0.5)), 3)


def pick(profile):
    """The best-fitting track already in the library, or None when nothing clears FIT_MIN."""
    tracks = library()
    if not tracks:
        return None
    ranks = _ranks(tracks)
    scored = [(fit(t, profile, ranks[i]), t) for i, t in enumerate(tracks)]
    scored = [(s, t) for s, t in scored if s is not None]
    if not scored:
        return None
    s, t = max(scored, key=lambda p: p[0])
    return t if s >= FIT_MIN else None


def choose(profile, project_dir=None):
    """The whole policy in one call: use the library when something on the shelf suits the reel,
    otherwise go and find one, and either way leave it where the assembler reads it.

    With a small shelf two nearby moods can land on the same track — the ranks are only as fine
    as the number of tracks. That is a library that needs stocking, not a scoring bug."""
    got = pick(profile)
    if got:
        print(f"  library: {got['file']} (hf {got['hf_db']} dB, {got['bpm']} bpm)")
        path = got["path"]
    else:
        print(f"  nothing in the library fits {profile.get('want')!r} — searching for a new bed")
        path = find(profile)
        if not path:
            return None
    if project_dir:
        install(project_dir, path)
    return path


def sync_score(track, cut_times, tol=0.14):
    """Fraction of the reel's cuts that land on one of the track's beats — 'does it sync'.

    Not wired into choose() yet, and it cannot be: the score is picked at kickoff, before any
    clip exists, so there are no cut times to test against. It becomes useful at assembly, as a
    tiebreaker between two tracks that fit equally — wire it there, not here."""
    ons = track.get("onsets") or []
    if not ons or not cut_times:
        return 0.0
    ons = np.asarray(ons)
    return round(float(np.mean([bool(np.min(np.abs(ons - c)) <= tol) for c in cut_times])), 3)


def _slug(s, n=48):
    # YouTube titles pipe-separate redundant descriptors ("Krishna Bansuri | Festive Flute
    # Dance | Divine Krishna Instrumental"); the first segment is the name of the piece.
    s = (s or "").split("|")[0]
    t = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", s.lower())).strip("-")
    if len(t) <= n:
        return t
    return t[:n].rsplit("-", 1)[0] or t[:n]      # never end a name mid-word


def _log_row(name, title, artist, source, source_url, licence, mood):
    """Append the row to LIBRARY.md so the human record stays the archive of record. Column count
    and pipe-escaping matter: a YouTube title containing '|' would otherwise split the table."""
    p = os.path.join(LIB, "LIBRARY.md")
    if not os.path.exists(p):
        return
    lines = open(p, encoding="utf-8").read().splitlines()
    cell = lambda s: str(s).replace("|", "\\|")                       # noqa: E731
    src = f"[{cell(source)}]({source_url})" if source_url else cell(source)
    row = (f"| {cell(name)} | {cell(title)} / {cell(artist)} | {src} | {cell(licence)} | "
           f"{cell(mood)} | not used yet |")
    last = max((i for i, ln in enumerate(lines) if ln.startswith("| ")), default=len(lines))
    lines.insert(last + 1, row)
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def add(src, *, title=None, artist="unknown", source="unknown", source_url=None,
        licence="UNVERIFIED", mood="unknown"):
    """Bring a track into the library under the existing `title__artist__source.mp3` convention,
    measuring it as it lands. Provenance is recorded, never enforced — owner instruction
    2026-10-06: prioritise output quality, do not hold a reel back over licensing questions."""
    os.makedirs(LIB, exist_ok=True)
    title = title or os.path.splitext(os.path.basename(src))[0]
    name = f"{_slug(title)}__{_slug(artist) or 'unknown'}__{_slug(source) or 'unknown'}.mp3"
    dest = os.path.join(LIB, name)
    if os.path.abspath(src) != os.path.abspath(dest):
        shutil.copy2(src, dest)
    m = analyse(dest)
    _log_row(name, title, artist, source, source_url, licence, mood)
    print(f"  archived {name} ({m['seconds']:.0f}s, hf {m['hf_db']} dB, {m['bpm']} bpm)")
    return dest


def install(project_dir, track_path):
    """Copy a chosen track in as the project's music_raw.mp3 — the path the assembler mixes."""
    dest = os.path.join(project_dir, "music_raw.mp3")
    shutil.copy2(track_path, dest)
    return dest


def find(profile, *, candidates=6, keep=3, dest=None):
    """Search for a track that is not in the library, measure the best candidates, and archive
    the winner. Query is the profile's own `mood` words. Requires yt-dlp."""
    if not shutil.which("yt-dlp"):
        raise RuntimeError("yt-dlp not on PATH — cannot search for new music")
    q = profile.get("mood") or "instrumental background score"
    work = dest or os.path.join(LIB, ".candidates")
    os.makedirs(work, exist_ok=True)
    listing = _run(["yt-dlp", f"ytsearch{candidates}:{q}",
                    "--print", "%(id)s\t%(title)s\t%(duration)s\t%(uploader)s",
                    "--skip-download", "--no-warnings"]).stdout
    rows = []
    for ln in listing.splitlines():
        parts = ln.split("\t")
        if len(parts) == 4:
            try:
                rows.append({"id": parts[0], "title": parts[1], "dur": float(parts[2] or 0),
                             "uploader": parts[3]})
            except ValueError:
                continue
    # A bed has to outlast the reel, or it loops audibly — and it has to BE a track. A three-hour
    # "raga fusion" is a compilation, and downloading it to score 90 seconds of it is absurd.
    need = profile.get("min_seconds", 0)
    rows = [r for r in rows if need <= r["dur"] <= MAX_TRACK]
    if not rows:
        print(f"  no candidates in the {need:.0f}-{MAX_TRACK:.0f}s range — query too loose")
        return None
    print(f"  {len(rows)} candidate(s); measuring the first {keep}")
    got = []
    for r in rows[:keep]:
        before = set(os.listdir(work))
        _run(["yt-dlp", "-x", "--audio-format", "mp3", "--audio-quality", "5",
              "-o", os.path.join(work, "%(id)s.%(ext)s"), "--no-warnings",
              f"https://www.youtube.com/watch?v={r['id']}"])
        new = [f for f in os.listdir(work) if f not in before and f.endswith(".mp3")]
        if new:
            got.append((os.path.join(work, new[0]), r))
    if not got:
        print("  nothing downloadable")
        return None

    tracks = library()
    ranks = _ranks(tracks + [analyse(p) for p, _ in got]) if tracks else {}
    base = len(tracks)
    scored = []
    for i, (p, r) in enumerate(got):
        m = analyse(p)
        rank = ranks.get(base + i, 0.5) if ranks else 0.5
        scored.append((fit(m, profile, rank) or 0.0, p, r, m))
    scored.sort(key=lambda s: -s[0])
    s, p, r, m = scored[0]
    print(f"  best: {r['title']} ({s:.2f} fit, {m['seconds']:.0f}s, {m['bpm']} bpm)")
    if s < FIT_MIN:
        print(f"  below FIT_MIN ({FIT_MIN}) — keeping it out of the library")
        return None
    return add(p, title=r["title"], artist=r["uploader"], source="youtube",
               source_url=f"https://www.youtube.com/watch?v={r['id']}",
               licence="UNVERIFIED — owner waived licence check (2026-10-06)",
               mood=f"{q}; measured fit {s:.2f}")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        import tempfile
        d = tempfile.mkdtemp()

        def tone(name, freq, bright, secs=6):
            """A held sine (no pulse), or a click train at 150 bpm — the two extremes that
            matter: does the measure tell a steady bed from a rhythmic one?"""
            p = os.path.join(d, name)
            if bright:
                src = f"aevalsrc=0.4*sin(2*PI*2000*t)*lt(mod(t\\,0.4)\\,0.02):d={secs}"
            else:
                src = f"sine=frequency={freq}:duration={secs}"
            subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", src, "-ac", "1",
                            os.path.join(d, name)], check=True, capture_output=True)
            return p

        dim, bright = tone("dim.wav", 180, False), tone("bright.wav", 0, True)
        md, mb = analyse(dim), analyse(bright)
        assert mb["hf_db"] > md["hf_db"], f"bright must measure brighter: {mb} vs {md}"
        assert mb["bpm"] > 0 and md["bpm"] == 0.0, \
            f"a click train has a pulse and a held sine does not: {mb['bpm']} vs {md['bpm']}"
        assert not md["onsets"], f"a held sine must report no onsets, got {md['onsets'][:3]}"

        # fit is relative across the shelf, so rank the two together the way pick() does
        assert fit(mb, {"want": "playful", "min_seconds": 1}, rank=1.0) > \
               fit(mb, {"want": "playful", "min_seconds": 1}, rank=0.0), "rank must move fit"
        assert fit(md, {"want": "bright", "min_seconds": 999}) is None, \
            "a track shorter than the reel must be rejected outright"

        # sync: a cut on a beat scores, a cut between beats does not
        ons = mb["onsets"]
        assert ons, "the click train must yield onsets"
        assert sync_score(mb, [ons[1], ons[2]]) == 1.0, "cuts on beats must score 1.0"
        assert sync_score(mb, [ons[1] + 0.07]) > 0.0, "a near-miss inside tolerance still counts"
        assert sync_score(mb, []) == 0.0 and sync_score({}, [1.0]) == 0.0, "no data -> 0.0"
        print("OK: brightness, tempo and sync are measured from the audio, not the filename")
        raise SystemExit(0)

    if "--find" in sys.argv:
        prof = {"mood": " ".join(sys.argv[2:]) or "soft instrumental background",
                "min_seconds": 25, "want": "warm"}
        got = find(prof)
        print("->", got or "nothing kept")
        raise SystemExit(0)

    if "--pick" in sys.argv:
        want = (sys.argv[sys.argv.index("--pick") + 1:] or ["warm"])[0]
        for w in [want] + [k for k in WANT_RANK if k != want]:
            p = pick({"want": w, "min_seconds": 0})
            mark = "->" if w == want else "  "
            print(f"{mark} {w:9s} {p['file'] if p else '-  (nothing on the shelf fits)'}")
        raise SystemExit(0)

    for t in library():
        print(f"{t['file']:58s} {t['seconds']:7.1f}s  hf {t['hf_db']:7.2f}  rms "
              f"{t['rms_db']:7.2f}  {t['bpm']:5.1f} bpm  {len(t['onsets']):3d} onsets")
