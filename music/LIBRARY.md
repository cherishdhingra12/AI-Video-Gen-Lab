# Music library

Copyright-free background tracks for the reels. Every reel that gets a score has its track
archived here with source + license, so provenance is never lost.

**The process (do this every time a reel gets music):**
1. Grab a copyright-free instrumental (Pixabay = no attribution; YouTube Audio Library;
   Incompetech = CC-BY; Free Music Archive). Log its source URL + license — don't skip this.
2. Copy the file here named `title__artist__source.mp3` and add a row to the table below.
3. Copy it into the project as `<project>/music_raw.mp3` (assemble_veo.py reads that path and
   mixes it as a ducked bed). Projects stay self-contained; this folder is the archive of record.

**As of 2026-10-08 this is automated — `python3 automation/music.py`:** the pipeline calls
`music.choose()` at kickoff with the mood the reel's own shot list declares (`music_mood` /
`music_want`). It scores the shelf by MEASURED fit, and only when nothing fits does it search
for a new track, download the candidates, measure them, and archive the winner here with its
row appended automatically. `python3 automation/music.py` lists the shelf with its measured
profile; `--pick <mood>` shows which track wins for each mood. The three steps below remain the
manual fallback and the explanation of what the automation is doing.

| File | Title / Artist | Source | License | Mood | Used in |
|------|----------------|--------|---------|------|---------|
| memory-of-the-village__desifreemusic__pixabay.mp3 | Memory of the Village – Emotional Folk Instrumental / DesiFreeMusic | [Pixabay](https://pixabay.com/music/search/memory%20of%20the%20village/) | Pixabay Content License — free commercial, **no attribution required** | warm Indian folk, bittersweet→hopeful; brightest/most percussive in the library (0–4 kHz energy −42 dB vs −48/−49 for the others) | janmadin (2:59), **aawaaz**, **filter_wala** (both ducked at MUS_VOL 0.30), **dadaji-ka-kahani** (3-D re-render, bed 0.5 → floor −39.3 dB, above the −46 floor gate) |
| wistful-warm-bed__source-unverified.mp3 | *unknown* | ⚠️ **UNVERIFIED** | ⚠️ **UNVERIFIED — confirm before publishing** | soft wistful→warm instrumental | intezaar, ek_ghanta (3:08, 256 kbps, 6027264 B) |
| emotional-sentimental-music__andriih__pixabay.mp3 | Emotional Sentimental Music / andriih | [Pixabay](https://pixabay.com/music/solo-piano-emotional-sentimental-music-601639/) | Pixabay Content License — free commercial, **no attribution required** | tender solo piano, bittersweet→warm | kitni_baar (2:24, 256 kbps), haath_thaame (30.67s, ducked bed) |
| bansuri-raag-des__rupak-kulkarni__archive-org.mp3 | Raag Des (bansuri) / Rupak Kulkarni | [archive.org/Rupak-Kulkarni-Bansuri](https://archive.org/details/Rupak-Kulkarni-Bansuri) | item declares CC Public Domain Mark (uploader-asserted; owner waived license check 2026-10-01) | solo bansuri, evening raga of waiting/longing — mournful→tender | roz_ki_list (36s window, from t=0 alap), **the-lamp-s-silent-promise** (ducked bed, MUS_VOL 0.30) |
| krishna-bansuri__mayar-krishna__youtube.mp3 | Krishna Bansuri \| Festive Flute Dance \| Divine Krishna Instrumental / Mayar Krishna | [youtube](https://www.youtube.com/watch?v=6iIQqJCe3b0) | UNVERIFIED — owner waived licence check (2026-10-06) | playful indian folk dholak festive instrumental; measured fit 0.92 | not used yet |
| bamboo-flute-music__bamboooye-archive-org__archive-org.mp3 | Bamboo Flute Music / BambooOye (archive.org) | [archive.org](https://archive.org/details/bamboo-flute-music-bamboo-oye) | CC0 1.0 Public Domain — no attribution required | soft bamboo flute; warm/neutral bed | not used yet |
| tabla-solo-folk-matsya-tabla-by-joydeep__joydeep-archive-org__archive-org.mp3 | Tabla Solo (folk/matsya tabla, by joydeep) / joydeep (archive.org) | [archive.org](https://archive.org/details/10-oct-9.05-pm-pitch-2.30-tempo-121) | CC0 1.0 Public Domain — no attribution required | energetic tabla solo, bright/playful percussion; festival energy | not used yet |
| folk-music-india-dholak__indian-sounds-classical-music__youtube.mp3 | Folk Music India Dholak / Indian Sounds & Classical Music | [youtube](https://www.youtube.com/watch?v=298UX9HxB4s) | UNVERIFIED — owner waived licence check (2026-10-06) | playful Indian folk dholak upbeat happy; measured fit 0.98 | not used yet |

⚠️ **`wistful-warm-bed`**: this track was grabbed in an earlier session with no source/license
recorded, and no metadata survives in the file. intezaar + ek_ghanta both use it. Its source
must be identified and its license confirmed **before either reel is published** — a wrong or
missing license is a takedown/strike risk. Only the person who downloaded it can likely recall
where it came from.

**Score selection is measured, not guessed (2026-10-06).** A reel's score must fit the reel's
essence, so the tracks are compared on high-frequency content (percussive/folk energy) rather
than picked by name: `bansuri-raag-des` is the dimmest (−49.5 dB above 4 kHz) and is a *mournful
evening raga*, which was rejected for the comedy reel फ़िल्टर — a solemn bansuri under a bunny-ear
joke reads as the wrong film. Both new reels use `memory-of-the-village`, the warmest and most
upbeat track available. Owner instruction 2026-10-06: prioritise output quality; do not hold a
reel back over licensing questions. Licensing rows are still recorded above for provenance.
