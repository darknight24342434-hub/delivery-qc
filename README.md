# delivery-qc

Read-only pre-delivery QC for finished MP4 and MOV files, producing JSON and Markdown reports with CI-friendly exit codes.

## What it does / why

Before a video goes out the door there is a short list of things that are cheap to check and expensive to miss: the file does not decode, the loudness is off target, a black or frozen segment slipped in, the codec is not what the delivery spec asked for, or a subtitle track is present but empty. `delivery_qc.py` runs those checks in one pass over one or many files and writes a report you can attach to a delivery or gate a pipeline on.

The tool is strictly read-only with respect to the media. It never writes into or beside the input paths; the only files it creates are the two reports in `--out-dir`.

Checks performed per file:

- **Metadata** — duration, resolution, video/audio codec, stream presence.
- **Decode pass** — runs `ffmpeg` to decode the mapped video and audio streams to the null muxer, so a file that is structurally broken mid-stream is caught rather than trusted on its header.
- **Loudness** — integrated LUFS and loudness range via the `ebur128` filter. If `ebur128` produces no integrated value, it falls back to `volumedetect` mean/max dBFS and records the fallback reason in the report.
- **Visual anomalies** — `blackdetect` and `freezedetect`, with per-segment durations compared against the spec limits.
- **Subtitles** — presence of subtitle streams, plus packet-timing basics when subtitle streams exist and `ffprobe` is available.
- **Container/codec compliance** — separate allowlists for `.mp4` and `.mov`, overridable from the spec file.
- **Spec comparisons** — optional expected duration (with tolerance), expected resolution, and LUFS window.

## Requirements

- Python 3.10 or newer. Standard library only — no third-party packages.
- `ffmpeg` on `PATH` for the decode, loudness and visual checks.
- `ffprobe` on `PATH` for structured metadata and subtitle packet timing. If `ffprobe` is missing, the tool locates it next to `ffmpeg` if possible, and otherwise falls back to parsing `ffmpeg -i` stderr for duration, streams, codecs and resolution. Checks that need packet-level data then degrade with a warning rather than failing the run.

## Install

No install step. Clone the repository and run the script:

```
git clone <repo-url>
cd delivery-qc
python delivery_qc.py --help
```

## Usage

Check explicit files:

```
python delivery_qc.py finished.mp4 finished.mov --out-dir ./reports
```

Scan a folder recursively for `.mp4` and `.mov`:

```
python delivery_qc.py --scan-root /path/to/project --out-dir ./reports
```

Apply a delivery spec:

```
python delivery_qc.py finished.mp4 --out-dir ./reports --spec examples/delivery_spec.json
```

Full flag list:

| Flag | Default | Meaning |
| --- | --- | --- |
| `inputs` (positional) | — | One or more MP4/MOV files, or directories to scan recursively. |
| `--scan-root PATH` | — | Recursively scan this root for `.mp4` and `.mov`. Can be combined with positional inputs. |
| `--out-dir PATH` | `delivery_qc_reports` | Directory the two reports are written to. Created if missing. |
| `--spec PATH` | — | JSON delivery spec; keys override the built-in defaults. |
| `--timeout SECONDS` | `900` | Per-command timeout for each `ffmpeg`/`ffprobe` invocation. |
| `--fail-on {fail,warn,never}` | `fail` | Controls the process exit code (see below). |

### Spec file

Every key is optional; anything omitted keeps the built-in default. See `examples/delivery_spec.json`:

```json
{
  "expected_duration_sec": 42.0,
  "duration_tolerance_sec": 0.5,
  "width": 1920,
  "height": 1080,
  "require_audio": true,
  "lufs_min": -24.0,
  "lufs_max": -14.0,
  "max_black_segment_sec": 3.0,
  "max_freeze_segment_sec": 3.0,
  "mp4_video_codecs": ["h264", "hevc"],
  "mp4_audio_codecs": ["aac", "alac", "mp3"],
  "mov_video_codecs": ["h264", "hevc", "prores"],
  "mov_audio_codecs": ["aac", "alac", "pcm_s16le", "pcm_s24le", "pcm_s32le"]
}
```

Built-in defaults also include `allowed_extensions` (`.mp4`, `.mov`), `black_detect_min_duration_sec` (`0.5`) and `freeze_detect_min_duration_sec` (`2.0`).

## Output

Each run writes two timestamped files into `--out-dir`:

- `delivery_qc_YYYYMMDD_HHMMSS.json` — the full machine-readable report: run metadata, which tools were found, the effective spec, a per-file record with every check's raw result and issue list, and a `pass`/`warn`/`fail` summary count.
- `delivery_qc_YYYYMMDD_HHMMSS.md` — the same run rendered as a human-readable Markdown summary.

Both paths are printed to stdout along with the summary line, e.g. `Summary: pass=3 warn=1 fail=0`.

Exit codes, controlled by `--fail-on`:

- `--fail-on fail` (default): `0` when no file failed, `2` when at least one file failed. Warnings do not affect the exit code.
- `--fail-on warn`: `1` when at least one file has a warning or a failure, `0` otherwise.
- `--fail-on never`: always `0`, so reports are still generated without breaking a batch script.

An unhandled error (no inputs found, unreadable spec) prints `delivery_qc error: ...` to stderr and exits `2`.

## Limitations

- Everything of substance depends on `ffmpeg`. With neither `ffmpeg` nor `ffprobe` present the report is still written and carries a run-level `tool_warning`, but every file is marked `fail` on the `probe` check — so a CI job on a machine without ffmpeg exits `2` under the default `--fail-on fail`, it does not quietly pass.
- Without `ffprobe`, metadata comes from parsing `ffmpeg -i` stderr. That is a best-effort text parse: it recovers duration, resolution and codec names, but subtitle packet timing and other packet-level checks are unavailable and are reported as warnings.
- Only `.mp4` and `.mov` are recognised. Other containers are not discovered by `--scan-root` and are not covered by the codec allowlists.
- `freezedetect` and `blackdetect` are heuristics with fixed thresholds (`pic_th=0.98` for black, `n=-60dB` for freeze). Intentional black frames or genuinely static shots will be reported; the minimum-duration knobs are the way to tune this down.
- Loudness is measured over the whole programme. There is no true-peak limiter check and no per-segment or dialogue-gated measurement.
- Long files multiply the runtime: the decode pass, the loudness pass and the anomaly pass each read the file once. `--timeout` applies per command, not to the whole run.

## License

MIT. See [LICENSE](LICENSE).
