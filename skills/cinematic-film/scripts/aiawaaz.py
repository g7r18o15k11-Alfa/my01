#!/usr/bin/env python3
"""Ai Awaaz (aiawaaz.io) as the film's voice: export paste-ready scripts, import the MP3s you download.

Ai Awaaz has no public API (checked November 2026): voices are made in its web app
(https://app.aiawaaz.io), which takes up to 2,000 characters and 20 lines per audio and exports MP3.
So the bridge has two steps around the app:

  python3 aiawaaz.py export --project my-film
      Writes audio/aiawaaz/<take>.txt (or <take>__p1.txt, __p2 ... when a take is over the limits),
      one narration line per paragraph, plus audio/aiawaaz/UPLOAD.md: the order, the character
      count of every file and the total against your monthly allowance.

  (In the app: paste each file, use the SAME voice, emotion, pitch and speed for every file, and
   download each MP3 with the same name as its .txt, into audio/aiawaaz/downloads/.)

  python3 aiawaaz.py import --project my-film
      Joins the parts of each take, converts to 48 kHz mono WAV and writes audio/takes/<take>.wav,
      then checks each take has enough pauses for split_takes.py to cut it into lines.

Then carry on as with any voice: split_takes.py, then build_narration.py.
"""
import argparse
import json
import pathlib
import re
import subprocess
import sys
import tempfile

MAX_CHARS = 2000            # Ai Awaaz: characters in one audio
MAX_LINES = 20              # Ai Awaaz: lines in one audio
AUDIO_EXT = (".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac")
NOISE_DB, MIN_SIL = -40, 0.16   # must match split_takes.py


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"FAILED: {' '.join(map(str, cmd))}\n{r.stderr[-1500:]}")
    return r


def duration(path):
    return float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)]).stdout.strip())


def chars(text):
    """Characters as Ai Awaaz counts them: tags in [square brackets] are free."""
    return len(re.sub(r"\[[^\]]*\]", "", text))


def parts_of(take, max_chars, max_lines):
    """Split one take's lines into the fewest paste-sized parts, never breaking a line."""
    parts, cur, n = [], [], 0
    for l in take["lines"]:
        c = chars(l["spoken"].strip())
        if c > max_chars:
            sys.exit(f"line {l['id']} is {c} characters; Ai Awaaz takes {max_chars} at most. Split the line in script.json.")
        if cur and (n + c + 2 > max_chars or len(cur) >= max_lines):
            parts.append(cur); cur, n = [], 0
        cur.append(l); n += c + (2 if len(cur) > 1 else 0)
    if cur:
        parts.append(cur)
    return parts


def names(take, parts):
    t = take["take"]
    return [t] if len(parts) == 1 else [f"{t}__p{i}" for i in range(1, len(parts) + 1)]


def cmd_export(a, proj, script):
    out = proj / "audio" / "aiawaaz"; out.mkdir(parents=True, exist_ok=True)
    (out / "downloads").mkdir(exist_ok=True)
    for old in out.glob("*.txt"):
        old.unlink()
    rows, total, manifest = [], 0, {}
    for take in script["takes"]:
        parts = parts_of(take, a.max_chars, a.max_lines)
        manifest[take["take"]] = []
        for name, part in zip(names(take, parts), parts):
            text = "\n\n".join(l["spoken"].strip() for l in part) + "\n"
            (out / f"{name}.txt").write_text(text, encoding="utf-8")
            c = chars(text.strip()); total += c
            rows.append((name, len(part), c, f"{part[0]['id']}-{part[-1]['id']}"))
            manifest[take["take"]].append({"file": name, "spoken": [l["spoken"].strip() for l in part]})
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    w = max(len(r[0]) for r in rows)
    md = ["# Ai Awaaz upload list", "",
          "Paste each file into the Ai Awaaz app **in this order**, with the **same voice, emotion, pitch and speed** every time.",
          f"Download each MP3 as `<file name>.mp3` into `{(out / 'downloads').as_posix()}`, then run `aiawaaz.py import`.", "",
          "Keep a clear pause between lines: one narration line per paragraph, as in the files. If the app reads two",
          "lines with no pause, split_takes.py will flag them and you can force the cut in audio/overrides.json.", "",
          "| # | File | Lines (ids) | Characters |", "|---|---|---|---|"]
    for i, (n, k, c, ids) in enumerate(rows, 1):
        md.append(f"| {i} | `{n}.txt` | {k} ({ids}) | {c:,} |")
    md += ["", f"**Total: {total:,} characters**" + (f" of your {a.monthly:,} monthly allowance ({100 * total / a.monthly:.0f}%)." if a.monthly else "."),
           "Every re-record of a take costs its characters again."]
    (out / "UPLOAD.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    for i, (n, k, c, ids) in enumerate(rows, 1):
        print(f"{i:>3}  {n:<{w}}  {k:>2} lines  {c:>5} chars")
    print(f"total {total:,} characters -> {out}/UPLOAD.md")
    if a.monthly and total > a.monthly:
        print(f"WARNING: {total:,} characters is more than your {a.monthly:,} monthly allowance")


def find_audio(folder, name):
    hits = [p for p in folder.iterdir() if p.is_file() and p.stem.lower() == name.lower() and p.suffix.lower() in AUDIO_EXT]
    if len(hits) > 1:
        sys.exit(f"two files for {name}: {', '.join(h.name for h in hits)}; keep one")
    return hits[0] if hits else None


def pauses(wav, t0=0.05):
    err = run(["ffmpeg", "-v", "info", "-i", str(wav), "-af", f"silencedetect=noise={NOISE_DB}dB:d={MIN_SIL}", "-f", "null", "-"]).stderr
    total = duration(wav)
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", err)]
    return sum(1 for s in starts if t0 < s < total - 0.3)


def cmd_import(a, proj, script):
    src = pathlib.Path(a.src) if a.src else proj / "audio" / "aiawaaz" / "downloads"
    if not src.is_dir():
        sys.exit(f"no folder {src}; download the MP3s there, or pass --from <folder>")
    tdir = proj / "audio" / "takes"; tdir.mkdir(parents=True, exist_ok=True)
    only = {x.strip() for x in a.only.split(",") if x.strip()}
    mf = proj / "audio" / "aiawaaz" / "manifest.json"
    if not mf.exists():
        sys.exit("no audio/aiawaaz/manifest.json; run `aiawaaz.py export` first")
    manifest = json.loads(mf.read_text(encoding="utf-8"))
    expected, missing, todo = set(), [], []
    for take in script["takes"]:
        parts = manifest.get(take["take"])
        if parts is None or [x for p in parts for x in p["spoken"]] != [l["spoken"].strip() for l in take["lines"]]:
            sys.exit(f"take {take['take']} changed in script.json since the export; run export again and re-record that take")
        nm = [p["file"] for p in parts]
        expected |= {n.lower() for n in nm}
        if only and take["take"] not in only:
            continue
        files = [find_audio(src, n) for n in nm]
        missing += [f"{n}.mp3" for n, f in zip(nm, files) if f is None]
        todo.append((take, files))
    if missing:
        sys.exit("missing downloads in " + str(src) + ":\n  " + "\n  ".join(missing) +
                 "\nName each MP3 exactly like its .txt in audio/aiawaaz/ (run export again if script.json changed).")
    extra = [p.name for p in src.iterdir() if p.suffix.lower() in AUDIO_EXT and p.stem.lower() not in expected]
    for e in extra:
        print(f"note: {e} matches no take and was ignored")
    gap = f"{a.part_gap:.3f}"
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        for take, files in todo:
            out = tdir / f"{take['take']}.wav"
            if len(files) == 1:
                run(["ffmpeg", "-y", "-v", "error", "-i", str(files[0]), "-ar", "48000", "-ac", "1", str(out)])
            else:
                ins = []
                for f in files:
                    ins += ["-i", str(f)]
                n = len(files)
                fl = "".join(f"[{i}:a]aresample=48000,aformat=channel_layouts=mono,apad=pad_dur={gap}[a{i}];" for i in range(n - 1))
                fl += f"[{n - 1}:a]aresample=48000,aformat=channel_layouts=mono[a{n - 1}];"
                fl += "".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[o]"
                run(["ffmpeg", "-y", "-v", "error", *ins, "-filter_complex", fl, "-map", "[o]", "-ar", "48000", "-ac", "1", str(out)])
            k = len(take["lines"]); p = pauses(out)
            status = "ok" if p >= k - 1 else f"ONLY {p} PAUSES FOR {k} LINES: re-record with clearer pauses, or force cuts in audio/overrides.json"
            print(f"{take['take']:<20} {len(files)} file(s)  {k:>2} lines  {duration(out):6.1f}s  {p:>2} pauses  {status}")
    print(f"wrote {len(todo)} take(s) to {tdir}; next: split_takes.py, then build_narration.py")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["export", "import"])
    ap.add_argument("--project", required=True)
    ap.add_argument("--max-chars", type=int, default=MAX_CHARS, help="export: characters per Ai Awaaz audio (default 2000)")
    ap.add_argument("--max-lines", type=int, default=MAX_LINES, help="export: lines per Ai Awaaz audio (default 20)")
    ap.add_argument("--monthly", type=int, default=0, help="export: your monthly character allowance, e.g. 30000, 150000, 300000")
    ap.add_argument("--from", dest="src", default=None, help="import: folder with the downloaded files")
    ap.add_argument("--part-gap", type=float, default=0.8, help="import: silence between parts of one take (s)")
    ap.add_argument("--only", default="", help="import: comma-separated take names to (re)import")
    a = ap.parse_args()
    proj = pathlib.Path(a.project)
    script = json.loads((proj / "script.json").read_text(encoding="utf-8"))
    (cmd_export if a.action == "export" else cmd_import)(a, proj, script)


if __name__ == "__main__":
    main()
