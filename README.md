# mkv2mp4 - Guide

Converts MKV files to MP4 **without re-encoding the video** (no quality loss) and keeps your audio tracks, subtitles, languages, track names and chapters wherever MP4 allows it. There are no settings to pick: the script inspects every track, chooses the best action itself, and starts immediately.

- Command-line version: `mkv2mp4.py`  -  Window version: `mkv2mp4_gui.pyw` (each is a single, independent file)
- Needs: **Python 3.8+** and **ffmpeg** (includes ffprobe). No pip packages.

---

## 1. Install ffmpeg

| System | Command |
|---|---|
| Windows | `winget install Gyan.FFmpeg` (then open a **new** terminal) |
| macOS | `brew install ffmpeg` |
| Ubuntu / Debian | `sudo apt install ffmpeg` |
| Fedora | `sudo dnf install ffmpeg` |

Check: `ffmpeg -version` and `ffprobe -version`. If ffmpeg isn't on your PATH, put `ffmpeg` and `ffprobe` in the same folder as the script.

---

## 2a. The graphical version (no command line needed)

`mkv2mp4_gui.pyw` is the same converter in a window. It is **one file, uses only what ships with Python** (tkinter), and contains its own copy of the engine, so it doesn't need `mkv2mp4.py`.

**Start it:** double-click `mkv2mp4_gui.pyw` (the `.pyw` ending means no black console window on Windows), or run `python mkv2mp4_gui.pyw`.
**Shortcut:** drag `.mkv` files onto the `mkv2mp4_gui.pyw` icon and they are added to the list.

| Part of the window | What it does |
|---|---|
| Top right: ffmpeg status | Green dot = ffmpeg found. If missing on Windows, a **Download ffmpeg** button fetches it (100-200 MB, stored in your user profile, no admin rights). On macOS/Linux it shows install instructions. |
| **1 Choose your videos** | **Add files**, **Add folder**, **Remove**, **Clear all**. The checkbox is the `-r` option (search sub-folders when adding a folder). Each file shows its status (Waiting / Working / Done / Skipped / Failed). |
| **2 Where to save** | "Next to each original video" or "This folder" + **Browse** (the `-o` option). |
| **3 What will happen to each track** | Select a file to see every track and its action (COPY / CONVERT / RE-ENCODE / SAVE AS FILE / SKIP) with colour coding, summary chips for video / audio / subtitles, and a "why" box. Warnings (Dolby Vision, 10-bit H.264...) appear underneath. |
| **4 Convert** | **Start conversion**, **Stop** (cleans up the unfinished file), **Preview only** checkbox (the `--dry-run` option: shows the plan and the ffmpeg command in the log, writes nothing), progress bars for the current file (with ETA and speed) and for all files, and **Open output folder**. |
| **5 Activity log** | Colour-coded messages with hints for every problem. **Copy**, **Clear**, **Open log file**. |

The window uses a dark theme (and a dark title bar on Windows 10/11). Folder pickers and message boxes are drawn by the operating system, so they keep your system's own look.

The GUI makes the same automatic choices as the command-line script and has the same safety rules (never touches the original, never overwrites an MP4, writes to `.partial.mp4` first, verifies the result).
On Linux you may need `sudo apt install python3-tk` for the window to open.

---

## 2. Usage (the whole interface)

```
python mkv2mp4.py movie.mkv                  # convert one file (MP4 appears next to it)
python mkv2mp4.py D:\Videos -r -o D:\MP4s    # whole folder tree, keep the folder structure
python mkv2mp4.py "*.mkv"                    # wildcard
python mkv2mp4.py movie.mkv --dry-run        # only show the plan, change nothing
```

| Option | Meaning |
|---|---|
| `-r` | Also search sub-folders |
| `-o DIR` | Write MP4s into `DIR` (folder structure is kept with `-r`) |
| `--dry-run` | Show what would happen and the exact ffmpeg command, write nothing |
| `-h`, `--help`, `/?` | Show the built-in help with all options, examples and what is decided automatically |
| `--version` | Show the script version |

That's it. Everything else is automatic.

---

## 3. What it decides automatically

Example of the plan it prints before converting:

```
#0  video     hevc 3840x2160 yuv420p10le HDR10
     -> COPY: hevc fits in MP4 - copied untouched, tagged hvc1 for Apple devices
#1  audio     eng dts (DTS-HD MA) 5.1(side)
     -> CONVERT: dts isn't reliably supported in MP4 - converted to AAC 640k
#2  audio     eng aac stereo
     -> COPY: aac plays everywhere - copied untouched
#3  subtitle  eng subrip [default]
     -> CONVERT: text subtitle - embedded as an MP4 subtitle track
#4  subtitle  eng hdmv_pgs_subtitle
     -> SAVE AS FILE: picture subtitle can't go in MP4 - saved as a separate file
```

### Video
| Source codec | Action | Quality |
|---|---|---|
| H.264, HEVC, AV1, VP9, MPEG-4 | **Copied** | Identical, zero loss |
| Anything else (MPEG-2, VC-1, VP8, ...) | Re-encoded with x264 (CRF 14), or x265 (CRF 16) for 10-bit | Visually lossless, not bit-identical |

HEVC is tagged `hvc1` so iPhones, Macs and Apple TV can play it. Cover-art "video" streams are ignored.

### Audio
| Source codec | Action |
|---|---|
| AAC, MP3 | **Copied** untouched |
| Everything else (AC3, EAC3, DTS, TrueHD, FLAC, Opus, Vorbis...) | Converted to **AAC** at a high bitrate (128k mono, 256k stereo, 640k 5.1, 768k 7.1) |

Why not copy AC3/EAC3? They fit inside an MP4 file, but Chrome, Firefox and many web players (and some phones) can't decode them, and the video then refuses to play with an "unsupported audio codec" error. AAC and MP3 play everywhere.

Channel count is always kept (no forced downmix/upmix).

> **Honest note:** converting any non-AAC track to AAC is a re-encode, so a tiny loss is unavoidable (at these bitrates it is very hard to hear). If you only play on a TV or home-theatre setup that handles Dolby/DTS and you want the original audio bit-for-bit, keep the MKV for that.

### Subtitles
| Subtitle type | What happens |
|---|---|
| **Text** (SRT, WebVTT, ...) | Embedded as a selectable MP4 track |
| **ASS/SSA** (styled) | Embedded as a plain MP4 track **and** the original styled file is saved next to the MP4 (`Movie.eng.ass`) |
| **Picture** (PGS from Blu-ray, VobSub from DVD) | MP4 can't store these, so they're saved next to the MP4 as `.sup` (PGS) or `.mks` (others). Nothing is lost; players like Plex, Jellyfin and VLC load them automatically |

Side files are named like `Movie.eng.srt`, `Movie.eng.forced.sup`, `Movie.fra.sdh.ass` (duplicates get a number).

### Kept vs. not kept
- **Kept:** language tags, track titles, default/forced/SDH flags, chapters, file-level tags. The MP4 is written with `+faststart` so it starts playing quickly when streamed.
- **Not possible in MP4:** embedded fonts/attachments (shown as SKIP in the plan) and picture subtitles (saved as side files instead).

---

## 4. Safety features

- **Never touches your original MKV.** There is no "delete source" feature on purpose.
- **Never overwrites an existing MP4.** If `movie.mp4` already exists, that file is skipped and left alone (delete it if you want it redone). This also makes re-running a batch after an interruption safe.
- **Writes to a temporary `name.partial.mp4`** and renames it only after everything succeeded. A crash, power cut or Ctrl+C never leaves a half-finished file under the real name.
- **Checks free disk space** before starting.
- **Verifies the result:** the new MP4 is re-opened and compared with the plan (track counts) and the source (duration). Mismatches are reported as warnings.
- **Auto-rescue for subtitles:** if a subtitle track blocks the conversion, it retries once without embedded subtitles and saves them as separate files, so you still get your MP4.
- **A batch keeps going** if one file fails; a summary at the end lists what failed and why. Full ffmpeg output for failures goes to `mkv2mp4.log` in the folder you ran the script from.

Exit codes: `0` all good, `1` at least one file failed, `2` bad input or ffmpeg missing, `130` stopped with Ctrl+C.

---

## 5. Troubleshooting

| Message | Meaning and fix |
|---|---|
| `'ffmpeg' was not found on this computer` | Install ffmpeg (section 1), open a new terminal, or place ffmpeg/ffprobe next to the script. |
| `Path not found` | Typo, disconnected drive, or spaces without quotes: use `"C:\My Videos\file.mkv"`. |
| `No .mkv files found in this folder` | Add `-r` to include sub-folders. |
| `The file is empty (0 bytes)` | The copy/download failed. Get the file again. |
| `The file is damaged, incomplete, or not really a Matroska file` | Interrupted download/copy, or not an MKV. Check it plays in VLC; MKVToolNix can repair some files. |
| `Permission denied` / `The operating system refused access` | Output folder is read-only, or another program has the file open. Use `-o` with a folder you own; close players and sync tools. |
| `Not enough free disk space` | Free space or use `-o` on another drive. Re-encoded video can exceed the source size. |
| `This file has no usable video track` | Audio-only MKV. Extract audio with `ffmpeg -i in.mkv -vn -c:a copy out.m4a`. |
| `Your ffmpeg doesn't include the 'libx264' encoder` | Install a full ffmpeg build (Windows: gyan.dev "full"). |
| `One of the tracks uses a codec that MP4 cannot store` | Keep that file as MKV; MKV can hold everything. |
| `Your ffmpeg is too old` | Update to ffmpeg 4.4 or newer. |
| `... already exists - left untouched` | An MP4 with that name is already there. Delete or rename it to convert again. |
| `Length differs` (warning) | Output duration doesn't match the source; the source may be truncated. Compare in a player. |
| `H.264 ... yuv444p is valid but many TVs can't play it` (warning) | Copied correctly, but that video format has poor device support. |
| `Dolby Vision` (warning) | DV is kept only on ffmpeg 6.0+. Profile 5 may look purple/green on players without DV support. |

---

## 6. FAQ

**Is it really lossless?**
For video MP4 supports (H.264, HEVC, AV1, VP9, MPEG-4, which covers nearly all MKVs): yes, the video is copied, not re-encoded. Audio that's already AAC or MP3 is copied too. The only lossy steps are re-encoding unsupported video codecs and converting other audio formats to AAC. The plan always shows which.

**Why not just keep MKV?**
If the only goal is "everything preserved", MKV is the better container. MP4 is for device compatibility (TVs, iPhones, editors, browsers).

**My TV or phone won't play the MP4.**
Usually the codec rather than the container: 10-bit H.264, or HEVC video on browsers/devices without HEVC support. Check the warnings in the plan.

**I want burned-in subtitles or other custom behaviour.**
This version deliberately has no extra options. Picture subtitles are saved as side files, which keeps full quality and avoids re-encoding.
