#!/usr/bin/env python3
"""
mkv2mp4 GUI - convert MKV to MP4 with a point-and-click window.

Everything is in this one file and uses only what ships with Python (tkinter).
You also need ffmpeg: on Windows the program offers to download it for you.

  Start it:   double-click this file (Windows: .pyw runs without a black console window),
              or run   python mkv2mp4_gui.pyw
  Shortcut:   drag .mkv files onto this file's icon to add them straight away.

This file contains the same conversion engine as the command-line script mkv2mp4.py.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

__version__ = "1.0.0"

# --------------------------------------------------------------------------- #
#  Codec knowledge
# --------------------------------------------------------------------------- #
MKV_EXTENSIONS = {".mkv", ".mk3d"}

# Video codecs that ffmpeg can store in MP4 and that players widely understand.
MP4_VIDEO_COPY = {"h264", "hevc", "av1", "vp9", "mpeg4"}

# Audio codecs copied untouched. Only AAC and MP3 are decoded by every browser / web player / phone / TV.
# (AC3, E-AC3, ALAC, DTS... fit inside an MP4 file, but Chrome, Firefox and many web players can't play them.)
MP4_AUDIO_COPY = {"aac", "mp3"}

# Audio codecs that are lossless (only used to word the explanation).
LOSSLESS_AUDIO = {"flac", "truehd", "mlp", "alac"}

# Text-based subtitle codecs -> can become mov_text (the MP4 subtitle format).
TEXT_SUBS = {
    "subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text", "microdvd",
    "subviewer", "subviewer1", "sami", "realtext", "jacosub", "mpl2",
    "pjs", "stl", "vplayer",
}

# Picture-based subtitle codecs -> cannot live inside MP4.
IMAGE_SUBS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}

KIND_ORDER = {"video": 0, "audio": 1, "subtitle": 2}
KIND_LETTER = {"video": "v", "audio": "a", "subtitle": "s"}

INSTALL_HINT = (
    "Install ffmpeg (it includes ffprobe), then open a NEW terminal:\n"
    "      Windows : winget install Gyan.FFmpeg      (or download from https://www.gyan.dev/ffmpeg/builds/)\n"
    "      macOS   : brew install ffmpeg\n"
    "      Ubuntu/Debian : sudo apt install ffmpeg\n"
    "      Fedora  : sudo dnf install ffmpeg\n"
    "    Already installed but not found? Put ffmpeg(.exe) and ffprobe(.exe) in the same folder as this script."
)

# --------------------------------------------------------------------------- #
#  Console helpers (ASCII only so old Windows consoles never choke)
# --------------------------------------------------------------------------- #
USE_COLOR = False


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


def green(t): return _c("32", t)
def yellow(t): return _c("33", t)
def red(t): return _c("31", t)
def cyan(t): return _c("36", t)
def dim(t): return _c("2", t)
def bold(t): return _c("1", t)


class Cancelled(KeyboardInterrupt):
    """Raised when the user presses Stop in the GUI (or Ctrl+C in the terminal)."""


# Hooks used by the GUI. In the terminal they stay None and everything is printed.
CANCEL = threading.Event()
EMIT = None          # callable(kind, text): kind is info|ok|warn|fail|hint|dim|skip
PROGRESS_CB = None   # callable(seconds_done, total_seconds, speed_text)
PLAN_CB = None       # callable(plan)

# Never flash a console window when a GUI program starts ffmpeg; never wait for keyboard input.
_SUBPROC_KW = {"stdin": subprocess.DEVNULL}
if os.name == "nt":
    _SUBPROC_KW["creationflags"] = 0x08000000   # CREATE_NO_WINDOW


def emit(kind: str, text: str = "") -> None:
    if EMIT is not None:
        EMIT(kind, text)
        return
    styles = {
        "warn": lambda s: yellow(f"  [!] {s}"),
        "hint": lambda s: cyan(f"      Hint: {s}"),
        "fail": lambda s: red(f"  [X] {s}"),
        "ok": lambda s: green(f"  [OK] {s}"),
        "skip": lambda s: yellow(f"  [-] {s}"),
        "info": lambda s: cyan(f"  [i] {s}"),
        "dim": lambda s: dim(s),
    }
    print(styles.get(kind, lambda s: s)(text))


def say_warn(msg: str) -> None:
    emit("warn", msg)


def say_hint(msg: str) -> None:
    emit("hint", msg)


def say_fail(msg: str) -> None:
    emit("fail", msg)


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def human_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# --------------------------------------------------------------------------- #
#  Errors with hints
# --------------------------------------------------------------------------- #
class ConversionError(Exception):
    """A problem we can explain to the user in plain language."""

    def __init__(self, message: str, hint: Optional[str] = None, details: str = ""):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.details = details


# (regex, what happened, how to fix it, tag)
ERROR_RULES = [
    (r"No space left on device",
     "The disk ran out of space while writing the MP4.",
     "Free up space, or use -o to write the MP4 to a drive with more room.", "disk"),
    (r"Permission denied|Access is denied|Read-only file system",
     "The operating system refused access to a file or folder.",
     "Make sure the output folder isn't read-only, that no other program (media player, "
     "cloud-sync, antivirus) has the file open, and that you may write there. "
     "Use -o to choose a folder you own.", "perm"),
    (r"Unknown encoder|Encoder .* not found|Unknown decoder|Decoder .* not found",
     "Your ffmpeg build is missing a codec this file needs.",
     "Install a full ffmpeg build (Windows: gyan.dev 'full' build; macOS: brew install ffmpeg; "
     "Linux: install the 'ffmpeg' package from your distro).", "encoder"),
    (r"codec not currently supported in container|Could not find tag for codec|"
     r"not supported in container|incompatible with output codec|Tag .* incompatible",
     "One of the tracks uses a codec that MP4 cannot store.",
     "Check the stream plan above for the track that doesn't fit. "
     "For this file, MKV is the right container (MKV can hold everything).", "container"),
    (r"Too many packets buffered for output stream",
     "ffmpeg's muxing buffer overflowed (usually caused by a sparse subtitle track).",
     "Update ffmpeg to a recent version and try again.", "subs"),
    (r"(?i)subtitle|mov_text",
     "A subtitle track could not be converted to MP4.",
     "Text subtitles will be saved as separate .srt/.ass files instead; picture subtitles as .sup/.mks.", "subs"),
    (r"(?i)channel layout|channel_layout|Error while opening encoder for output stream .*audio",
     "An audio track has a channel layout the AAC encoder doesn't accept.",
     "Update ffmpeg. If it still fails, tell me the layout shown in the plan and convert that "
     "track to stereo manually with -ac 2.", "audio"),
    (r"Unrecognized option|Option .* not found",
     "Your ffmpeg is too old for one of the options this script uses.",
     "Update ffmpeg to version 4.4 or newer.", "old"),
    (r"EBML header parsing failed|moov atom not found|Invalid data found when processing input|"
     r"File ended prematurely|Input/output error|Duplicate element|exceeds containing master element",
     "The file is damaged, incomplete, or not really a Matroska (MKV) file.",
     "If it was downloaded or copied, the transfer may have been cut short - get it again. "
     "Check that it plays in VLC. MKVToolNix can sometimes repair broken MKVs.", "corrupt"),
    (r"No such file or directory",
     "ffmpeg could not find a file or folder it needed.",
     "The file may have been moved, renamed, or is on a disconnected drive.", "nofile"),
]


def explain_error(stderr: str) -> Tuple[str, str, str]:
    """Turn raw ffmpeg/ffprobe error text into (message, hint, tag)."""
    for pattern, message, hint, tag in ERROR_RULES:
        if re.search(pattern, stderr or ""):
            return message, hint, tag
    return ("ffmpeg stopped with an error it didn't explain clearly.",
            "See the 'ffmpeg said' lines below and the log file for the full output.", "generic")


def last_lines(text: str, n: int = 4) -> List[str]:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-n:]


# --------------------------------------------------------------------------- #
#  ffmpeg / ffprobe discovery
# --------------------------------------------------------------------------- #
@dataclass
class Tools:
    ffmpeg: str
    ffprobe: str
    version: str
    encoders: set


def user_bin_dir() -> Path:
    """Per-user folder where the GUI stores a downloaded ffmpeg (no admin rights needed)."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "mkv2mp4" / "bin"


def tool_folders() -> List[Path]:
    here = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) \
        else Path(__file__).resolve().parent
    return [here, user_bin_dir()]


def _find_tool(name: str, override: Optional[str]) -> str:
    if override:
        found = shutil.which(override) or (override if Path(override).is_file() else None)
        if not found:
            raise ConversionError(f"Could not find {name} at '{override}'.",
                                  "Check the path (point to the program file itself, not its folder).")
        return found
    found = shutil.which(name)
    if found:
        return found
    for folder in tool_folders():
        for candidate in (folder / name, folder / f"{name}.exe"):
            if candidate.is_file():
                return str(candidate)
    raise ConversionError(f"'{name}' was not found on this computer.", INSTALL_HINT)


def _run_text(cmd: List[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, **_SUBPROC_KW)


def load_tools(args) -> Tools:
    ffmpeg = _find_tool("ffmpeg", args.ffmpeg)
    ffprobe = _find_tool("ffprobe", args.ffprobe)
    try:
        version_out = _run_text([ffmpeg, "-hide_banner", "-version"]).stdout
        enc_out = _run_text([ffmpeg, "-hide_banner", "-encoders"]).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise ConversionError(f"ffmpeg was found but could not be started ({exc}).",
                              "Re-install ffmpeg, or check that the file is executable.")
    first = (version_out.splitlines() or ["ffmpeg (unknown version)"])[0]
    encoders = set(re.findall(r"^\s*[VAS][.A-Z]{5}\s+(\S+)", enc_out, re.M))
    return Tools(ffmpeg, ffprobe, first, encoders)


def require_encoder(tools: Tools, name: str, why: str) -> None:
    if tools.encoders and name not in tools.encoders:
        raise ConversionError(
            f"Your ffmpeg doesn't include the '{name}' encoder, which is needed {why}.",
            "Install a full ffmpeg build (Windows: gyan.dev 'full'; macOS: brew install ffmpeg; "
            "Linux: your distro's 'ffmpeg' package).")


# --------------------------------------------------------------------------- #
#  Stream model
# --------------------------------------------------------------------------- #
@dataclass
class Stream:
    index: int
    kind: str
    codec: str
    raw: dict
    lang: str = "und"
    title: str = ""
    default: bool = False
    forced: bool = False
    hearing_impaired: bool = False
    attached_pic: bool = False

    @staticmethod
    def from_probe(d: dict) -> "Stream":
        tags = {str(k).lower(): str(v) for k, v in (d.get("tags") or {}).items()}
        disp = d.get("disposition") or {}
        title = re.sub(r"[\x00-\x1f]", " ", tags.get("title", "")).strip()
        return Stream(
            index=int(d["index"]),
            kind=d.get("codec_type", "unknown"),
            codec=(d.get("codec_name") or "unknown").lower(),
            raw=d,
            lang=(tags.get("language") or "und").lower(),
            title=title,
            default=bool(disp.get("default")),
            forced=bool(disp.get("forced")),
            hearing_impaired=bool(disp.get("hearing_impaired")),
            attached_pic=bool(disp.get("attached_pic")),
        )

    @property
    def channels(self) -> int:
        try:
            return int(self.raw.get("channels") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def pix_fmt(self) -> str:
        return self.raw.get("pix_fmt") or ""

    @property
    def is_deep_color(self) -> bool:
        return any(t in self.pix_fmt for t in ("10le", "10be", "12le", "12be", "p010", "p012"))

    @property
    def is_cover_art(self) -> bool:
        if self.kind != "video":
            return False
        if self.attached_pic or self.codec in ("png", "bmp"):
            return True
        return self.codec == "mjpeg" and self.raw.get("avg_frame_rate") in (None, "", "0/0")

    @property
    def has_dolby_vision(self) -> bool:
        return any("DOVI" in str(sd.get("side_data_type", "")).upper()
                   for sd in (self.raw.get("side_data_list") or []))


def _fps(value) -> Optional[float]:
    try:
        num, den = str(value).split("/")
        return round(float(num) / float(den), 3) if float(den) else None
    except (ValueError, ZeroDivisionError):
        return None


def describe(s: Stream) -> str:
    r = s.raw
    flags = []
    if s.default:
        flags.append("default")
    if s.forced:
        flags.append("forced")
    if s.hearing_impaired:
        flags.append("SDH")
    flag_txt = f" [{', '.join(flags)}]" if flags else ""
    title_txt = f' "{s.title}"' if s.title else ""

    if s.kind == "video":
        bits = [s.codec, f"{r.get('width', '?')}x{r.get('height', '?')}", s.pix_fmt]
        fps = _fps(r.get("avg_frame_rate"))
        if fps:
            bits.append(f"{fps:g}fps")
        if r.get("color_transfer") == "smpte2084":
            bits.append("HDR10")
        elif r.get("color_transfer") == "arib-std-b67":
            bits.append("HLG")
        if s.has_dolby_vision:
            bits.append("DolbyVision")
        return " ".join(b for b in bits if b) + title_txt
    if s.kind == "audio":
        layout = r.get("channel_layout") or (f"{s.channels}ch" if s.channels else "?ch")
        profile = f" ({r['profile']})" if r.get("profile") and r.get("profile") != "unknown" else ""
        return f"{s.lang} {s.codec}{profile} {layout}{title_txt}{flag_txt}"
    if s.kind == "subtitle":
        return f"{s.lang} {s.codec}{title_txt}{flag_txt}"
    if s.kind == "attachment":
        name = (r.get("tags") or {}).get("filename", "")
        return f"{s.codec} {name}".strip()
    return s.codec


# --------------------------------------------------------------------------- #
#  The plan: what happens to each stream
# --------------------------------------------------------------------------- #
@dataclass
class Item:
    stream: Stream
    action: str            # copy | convert | reencode | extract | skip
    target: str = ""       # e.g. "aac", "mov_text", "libx264"
    reason: str = ""
    extra: str = ""
    params: dict = field(default_factory=dict)

    @property
    def embedded(self) -> bool:
        return self.action in ("copy", "convert", "reencode")


@dataclass
class Plan:
    items: List[Item] = field(default_factory=list)
    sidecars: List[Stream] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def video_reencode(self) -> bool:
        return any(i.action == "reencode" for i in self.items)

    def count(self, kind: str) -> int:
        return sum(1 for i in self.items if i.embedded and i.stream.kind == kind)


def aac_bitrate(channels: int, args) -> str:
    if args.audio_bitrate:
        return args.audio_bitrate
    table = {1: "128k", 2: "256k", 3: "320k", 4: "384k", 5: "512k", 6: "640k", 7: "704k", 8: "768k"}
    return table.get(channels, "256k")


def pick_video_encoder(s: Stream) -> str:
    return "libx265" if s.is_deep_color else "libx264"


def plan_video(s: Stream, plan: Plan) -> Item:
    if s.is_cover_art:
        return Item(s, "skip", reason="cover art / still picture, not a real video track")
    if s.codec in MP4_VIDEO_COPY:
        note = f"{s.codec} fits in MP4 - copied untouched (zero quality loss)"
        if s.codec == "hevc":
            note += ", tagged hvc1 for Apple devices"
        if s.has_dolby_vision:
            plan.warnings.append("This video has Dolby Vision data. Copying keeps it only if your ffmpeg is "
                                 "recent (6.0+). Profile 5 may look purple/green on players without DV support.")
        if s.codec == "h264" and (s.is_deep_color or "444" in s.pix_fmt or "422" in s.pix_fmt):
            plan.warnings.append(f"H.264 with pixel format '{s.pix_fmt}' is valid but many TVs, phones and "
                                 "browsers can't play it.")
        return Item(s, "copy", target="copy", reason=note)
    enc = pick_video_encoder(s)
    return Item(s, "reencode", target=enc,
                reason=f"{s.codec} can't be stored in MP4 - re-encoding with {enc} (visually lossless)")


def plan_audio(s: Stream, args) -> Item:
    if s.codec in MP4_AUDIO_COPY:
        return Item(s, "copy", target="copy",
                    reason=f"{s.codec} plays everywhere - copied untouched")
    ch = s.channels
    bitrate = aac_bitrate(ch, args)
    params = {"bitrate": bitrate}
    if ch > 8:
        params["ac"] = 8
    lossless = s.codec in LOSSLESS_AUDIO or s.codec.startswith("pcm_")
    if lossless:
        why = (f"{s.codec} is lossless but can't be played by browsers/web players inside MP4 - "
               f"converted to AAC {bitrate} (near-transparent)")
    else:
        why = (f"{s.codec} fits in MP4 but browsers and many web players can't decode it - "
               f"converted to AAC {bitrate} (lossy to lossy: small unavoidable loss)")
    return Item(s, "convert", target="aac", reason=why, params=params)


def plan_subtitle(s: Stream, plan: Plan, rescue: bool) -> Item:
    if s.codec in TEXT_SUBS:
        if rescue:
            plan.sidecars.append(s)
            return Item(s, "extract", reason="could not be embedded - saved as a separate subtitle file")
        item = Item(s, "convert", target="mov_text",
                    reason="text subtitle - embedded as an MP4 subtitle track")
        if s.codec in ("ass", "ssa"):
            plan.sidecars.append(s)
            item.reason += " (ASS styling is lost inside MP4)"
            item.extra = "original styled .ass also saved as a separate file"
        return item
    if s.codec in IMAGE_SUBS:
        plan.sidecars.append(s)
        return Item(s, "extract", reason="picture subtitle can't go in MP4 - saved as a separate file")
    return Item(s, "skip", reason=f"subtitle format '{s.codec}' isn't supported in MP4")


def build_plan(streams: List[Stream], args, rescue_subs: bool = False) -> Plan:
    plan = Plan()
    for s in streams:
        if s.kind == "video":
            plan.items.append(plan_video(s, plan))
        elif s.kind == "audio":
            plan.items.append(plan_audio(s, args))
        elif s.kind == "subtitle":
            plan.items.append(plan_subtitle(s, plan, rescue_subs))
        elif s.kind == "attachment":
            plan.items.append(Item(s, "skip", reason="attachment (fonts, images) - MP4 can't store these"))
        else:
            plan.items.append(Item(s, "skip", reason=f"'{s.kind}' data stream - not supported in MP4"))
    return plan


def print_plan(plan: Plan) -> None:
    if PLAN_CB is not None:
        PLAN_CB(plan)
        return
    print(bold("  Stream plan:"))
    colours = {"copy": green, "convert": yellow, "reencode": yellow, "extract": cyan, "skip": red}
    labels = {"copy": "COPY", "convert": "CONVERT", "reencode": "RE-ENCODE",
              "extract": "SAVE AS FILE", "skip": "SKIP"}
    for it in plan.items:
        s = it.stream
        print(f"   #{s.index:<2} {s.kind:<9} {describe(s)}")
        label = colours[it.action](labels[it.action])
        extra = f"  + {it.extra}" if it.extra else ""
        print(f"        -> {label}: {it.reason}{extra}")
    for w in plan.warnings:
        say_warn(w)


# --------------------------------------------------------------------------- #
#  Building the ffmpeg command
# --------------------------------------------------------------------------- #
def disposition_string(s: Stream) -> str:
    flags = []
    if s.default:
        flags.append("default")
    if s.forced:
        flags.append("forced")
    if s.hearing_impaired:
        flags.append("hearing_impaired")
    return "+".join(flags) or "0"


def codec_options(it: Item, letter: str, n: int, args) -> List[str]:
    s = it.stream
    c = f"-c:{letter}:{n}"
    if it.action == "copy":
        opts = [c, "copy"]
        if s.kind == "video" and s.codec == "hevc":
            opts += [f"-tag:v:{n}", "hvc1"]
        return opts
    if s.kind == "video":
        enc = it.target
        crf = args.crf if args.crf is not None else (16 if enc == "libx265" else 14)
        opts = [c, enc, f"-preset:v:{n}", args.preset, f"-crf:v:{n}", str(crf)]
        if s.pix_fmt:
            opts += [f"-pix_fmt:v:{n}", s.pix_fmt]
        if enc == "libx265":
            opts += [f"-tag:v:{n}", "hvc1", f"-x265-params:v:{n}", "log-level=error"]
        return opts
    if s.kind == "audio":
        opts = [c, "aac", f"-b:a:{n}", it.params["bitrate"]]
        if "ac" in it.params:
            opts += [f"-ac:a:{n}", str(it.params["ac"])]
        return opts
    return [c, "mov_text"]


def build_command(tools: Tools, args, src: Path, out: Path, plan: Plan) -> List[str]:
    cmd = [tools.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning", "-nostats",
           "-progress", "pipe:1", "-y", "-i", str(src)]

    counters = {"video": 0, "audio": 0, "subtitle": 0}
    maps: List[str] = []
    opts: List[str] = []
    ordered = sorted((i for i in plan.items if i.embedded and i.stream.kind in KIND_ORDER),
                     key=lambda i: (KIND_ORDER[i.stream.kind], i.stream.index))
    for it in ordered:
        s = it.stream
        letter = KIND_LETTER[s.kind]
        n = counters[s.kind]
        counters[s.kind] += 1
        maps += ["-map", f"0:{s.index}"]
        opts += codec_options(it, letter, n, args)
        opts += [f"-metadata:s:{letter}:{n}", f"language={s.lang}"]
        if s.title:
            opts += [f"-metadata:s:{letter}:{n}", f"handler_name={s.title}"]
        opts += [f"-disposition:{letter}:{n}", disposition_string(s)]

    cmd += maps + opts
    cmd += ["-map_metadata", "0", "-map_chapters", "0", "-movflags", "+faststart",
            "-max_muxing_queue_size", "9999", str(out)]
    return cmd


def printable(cmd: List[str]) -> str:
    return subprocess.list2cmdline(cmd) if os.name == "nt" else shlex.join(cmd)


# --------------------------------------------------------------------------- #
#  Running ffmpeg with a progress bar
# --------------------------------------------------------------------------- #
class Progress:
    def __init__(self, duration: float):
        self.duration = duration
        self.tty = sys.stdout.isatty()
        self.start = time.time()
        self.last_step = -1
        self.shown = False

    def update(self, cur_sec: float, speed: str) -> None:
        cols = max(40, shutil.get_terminal_size((100, 20)).columns - 1)
        if self.duration <= 0:
            if self.tty:
                print("\r" + f"  Working... {human_time(cur_sec)} processed".ljust(cols)[:cols],
                      end="", flush=True)
                self.shown = True
            return
        pct = max(0.0, min(100.0, cur_sec / self.duration * 100))
        if self.tty:
            width = 24
            filled = int(width * pct / 100)
            eta = ""
            if pct > 1:
                eta = f"  ETA {human_time((time.time() - self.start) * (100 - pct) / pct)}"
            line = f"  [{'#' * filled}{'-' * (width - filled)}] {pct:5.1f}%{eta}  {speed}"
            print("\r" + line.ljust(cols)[:cols], end="", flush=True)
            self.shown = True
        else:
            step = int(pct // 10) * 10
            if step > self.last_step:
                print(f"  {step}%", flush=True)
                self.last_step = step

    def finish(self) -> None:
        if self.tty and self.shown:
            cols = max(40, shutil.get_terminal_size((100, 20)).columns - 1)
            print("\r" + " " * cols + "\r", end="", flush=True)


def run_ffmpeg(cmd: List[str], duration: float) -> Tuple[int, str]:
    if CANCEL.is_set():
        raise Cancelled()
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace", bufsize=1, **_SUBPROC_KW)
    except OSError as exc:
        raise ConversionError(f"ffmpeg could not be started ({exc}).",
                              "Check that ffmpeg is installed and executable (see README).")
    err_lines: List[str] = []
    reader = threading.Thread(target=lambda: err_lines.extend(proc.stderr), daemon=True)
    reader.start()

    def watch_for_cancel() -> None:
        while proc.poll() is None:
            if CANCEL.is_set():
                proc.terminate()
                return
            time.sleep(0.2)
    threading.Thread(target=watch_for_cancel, daemon=True).start()

    progress = Progress(duration) if PROGRESS_CB is None else None
    state: dict = {}
    try:
        for line in proc.stdout:
            key, _, value = line.strip().partition("=")
            state[key] = value
            if key == "progress":
                raw = state.get("out_time_us") or state.get("out_time_ms") or ""
                try:
                    cur = int(raw) / 1_000_000
                except ValueError:
                    continue
                speed = state.get("speed", "").strip()
                if PROGRESS_CB is not None:
                    PROGRESS_CB(cur, duration, speed)
                else:
                    progress.update(cur, speed)
        proc.wait()
        if CANCEL.is_set():
            raise Cancelled()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        raise
    finally:
        if progress is not None:
            progress.finish()
    reader.join(timeout=5)
    return proc.returncode, "".join(err_lines)


# --------------------------------------------------------------------------- #
#  Probing, verification, side files
# --------------------------------------------------------------------------- #
def probe(tools: Tools, path: Path) -> dict:
    cmd = [tools.ffprobe, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", "-show_chapters", str(path)]
    try:
        result = _run_text(cmd, timeout=180)
    except subprocess.TimeoutExpired:
        raise ConversionError("Reading the file took too long and was stopped.",
                              "If the file is on a network drive or USB stick, copy it to a local disk first.")
    except OSError as exc:
        raise ConversionError(f"ffprobe could not be started ({exc}).", INSTALL_HINT)
    if result.returncode != 0:
        message, hint, _ = explain_error(result.stderr)
        raise ConversionError(message, hint, result.stderr)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        raise ConversionError("ffprobe returned unreadable information about this file.",
                              "Update ffmpeg and try again.", result.stdout[:500])


def verify_output(tools: Tools, out: Path, plan: Plan, src_duration: float) -> List[str]:
    problems: List[str] = []
    try:
        data = probe(tools, out)
    except ConversionError as exc:
        return [f"Could not re-open the new MP4 to double-check it: {exc.message}"]
    streams = [Stream.from_probe(s) for s in data.get("streams", [])]
    for kind in ("video", "audio", "subtitle"):
        have = sum(1 for s in streams if s.kind == kind)
        want = plan.count(kind)
        if have != want:
            problems.append(f"Expected {want} {kind} track(s) in the MP4 but found {have}.")
    try:
        out_duration = float((data.get("format") or {}).get("duration") or 0)
    except ValueError:
        out_duration = 0
    if src_duration > 0 and out_duration > 0:
        if abs(out_duration - src_duration) > max(2.0, src_duration * 0.01):
            problems.append(f"Length differs: source {human_time(src_duration)}, "
                            f"MP4 {human_time(out_duration)}. The source may be damaged or cut short.")
    return problems


def sidecar_spec(s: Stream) -> Tuple[str, List[str]]:
    if s.codec in IMAGE_SUBS:
        return (".sup" if s.codec == "hdmv_pgs_subtitle" else ".mks"), ["-c:s", "copy"]
    if s.codec in ("ass", "ssa"):
        return ".ass", ["-c:s", "ass"]
    if s.codec == "webvtt":
        return ".vtt", ["-c:s", "webvtt"]
    return ".srt", ["-c:s", "srt"]


def _extract_one(tools: Tools, src: Path, s: Stream, target: Path, codec_args: List[str]) -> Tuple[bool, str]:
    cmd = [tools.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", str(src),
           "-map", f"0:{s.index}"] + codec_args + [str(target)]
    try:
        result = _run_text(cmd, timeout=3600)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if result.returncode != 0:
        try:
            target.unlink()
        except OSError:
            pass
        return False, " | ".join(last_lines(result.stderr, 2))
    return True, ""


def extract_sidecars(tools: Tools, args, src: Path, dst: Path, streams: List[Stream]) -> List[str]:
    notes: List[str] = []
    used = set()
    for s in streams:
        ext, codec_args = sidecar_spec(s)
        base = dst.stem
        if s.lang not in ("und", ""):
            base += f".{s.lang}"
        if s.forced:
            base += ".forced"
        if s.hearing_impaired:
            base += ".sdh"
        name, k = base + ext, 2
        while name.lower() in used:
            name, k = f"{base}.{k}{ext}", k + 1
        used.add(name.lower())
        target = dst.with_name(name)
        if target.exists() and not args.overwrite:
            say_warn(f"Subtitle file already exists, left alone: {target.name} (delete it first if you want it replaced)")
            continue
        ok, why = _extract_one(tools, src, s, target, codec_args)
        if not ok and ext == ".sup":
            target = target.with_suffix(".mks")
            ok, why = _extract_one(tools, src, s, target, ["-c:s", "copy"])
        if ok:
            emit("ok", f"Saved subtitle track #{s.index} as {target.name}")
            notes.append(target.name)
        else:
            say_warn(f"Could not save subtitle track #{s.index} as a file ({why}). The MP4 itself is fine.")
    return notes


def write_log(args, src: Path, cmd: Optional[List[str]], details: str, note: str) -> Optional[Path]:
    path = Path(args.log).expanduser() if args.log else Path.cwd() / "mkv2mp4.log"
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"\n===== {dt.datetime.now():%Y-%m-%d %H:%M:%S} | {src}\n{note}\n")
            if cmd:
                fh.write(f"Command: {printable(cmd)}\n")
            if details:
                fh.write(f"--- ffmpeg output ---\n{details}\n")
        return path
    except OSError:
        return None


# --------------------------------------------------------------------------- #
#  Converting one file
# --------------------------------------------------------------------------- #
def check_disk_space(out_dir: Path, src_size: int, plan: Plan) -> None:
    probe_dir = out_dir
    while not probe_dir.exists() and probe_dir != probe_dir.parent:
        probe_dir = probe_dir.parent
    try:
        free = shutil.disk_usage(probe_dir).free
    except OSError:
        return
    need = int(src_size * 1.05)
    if free < need:
        raise ConversionError(
            f"Not enough free disk space: need about {human_size(need)}, only {human_size(free)} free.",
            "Free up space or use -o to write to another drive.")
    if plan.video_reencode and free < src_size * 2:
        say_warn(f"Video will be re-encoded; the MP4 may be larger than the source. "
                 f"Only {human_size(free)} free.")


def analyze(tools: Tools, args, src: Path):
    """Read the MKV and decide what happens to every track.
    Returns (streams, plan, duration_seconds, size_bytes). Raises ConversionError with a hint."""
    if src.suffix.lower() not in MKV_EXTENSIONS:
        say_warn(f"'{src.name}' doesn't end in .mkv - trying anyway.")
    try:
        size = src.stat().st_size
    except OSError as exc:
        raise ConversionError(f"Can't read the file ({exc}).", "Check the path and permissions.")
    if size == 0:
        raise ConversionError("The file is empty (0 bytes).", "The copy or download probably failed - get it again.")
    if not os.access(src, os.R_OK):
        raise ConversionError("You don't have permission to read this file.",
                              "Check the file's permissions, or whether another program locked it.")
    try:
        data = probe(tools, src)
    except ConversionError as exc:
        log_path = write_log(args, src, None, exc.details, "Could not read the file.")
        exc.details = "\n".join(last_lines(exc.details, 3)) + (f"\nFull details: {log_path}" if log_path else "")
        raise
    fmt = data.get("format") or {}
    if "matroska" not in str(fmt.get("format_name", "")).lower():
        say_warn(f"ffprobe sees this as '{fmt.get('format_name', 'unknown')}', not Matroska. Continuing anyway.")
    try:
        duration = float(fmt.get("duration") or 0)
    except ValueError:
        duration = 0.0
    streams = [Stream.from_probe(s) for s in data.get("streams", [])]
    if not streams:
        raise ConversionError("No tracks were found inside this file.", "The file may be empty or corrupt.")

    plan = build_plan(streams, args)
    if plan.count("video") == 0:
        raise ConversionError(
            "This file has no usable video track, so there is nothing to put in an MP4.",
            "For audio-only files, extract the audio instead, e.g. "
            "ffmpeg -i in.mkv -vn -c:a copy out.m4a")
    if plan.count("audio") == 0 and any(s.kind == "audio" for s in streams):
        plan.warnings.append("No audio track made it into the plan - the MP4 would be silent.")

    if any(i.target == "aac" for i in plan.items if i.embedded):
        require_encoder(tools, "aac", "to convert audio")
    for enc in {i.target for i in plan.items if i.embedded and i.target in ("libx264", "libx265")}:
        require_encoder(tools, enc, "to re-encode the video")
    return streams, plan, duration, size


def convert_one(tools: Tools, args, src: Path, dst: Path) -> Tuple[str, str]:
    """Returns (status, message). status: ok | skipped | dryrun. Raises ConversionError on failure."""
    if CANCEL.is_set():
        raise Cancelled()
    if dst.exists() and not args.overwrite:
        return "skipped", f"{dst.name} already exists - left untouched (delete or rename it to convert again)"
    streams, plan, duration, size = analyze(tools, args, src)

    print_plan(plan)
    if not args.dry_run:
        dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.stem + ".partial.mp4")
    cmd = build_command(tools, args, src, tmp, plan)

    if args.dry_run:
        emit("dim", "  Command that would run:")
        emit("dim", "  " + printable(cmd))
        return "dryrun", "dry run - nothing written"
    if args.verbose:
        emit("dim", "  " + printable(cmd))

    check_disk_space(dst.parent, size, plan)

    # Convert (with one automatic retry that rescues subtitles as side files)
    rescued = False
    try:
        if tmp.exists():
            tmp.unlink()
        started = time.time()
        rc, stderr = run_ffmpeg(cmd, duration)
        if rc != 0:
            message, hint, tag = explain_error(stderr)
            if tag == "subs" and plan.count("subtitle") > 0:
                say_warn("A subtitle track blocked the conversion. Retrying without embedded subtitles "
                         "(text subtitles will be saved as separate files instead)...")
                write_log(args, src, cmd, stderr, "First attempt failed (subtitle problem); retrying.")
                if tmp.exists():
                    tmp.unlink()
                plan = build_plan(streams, args, rescue_subs=True)
                cmd = build_command(tools, args, src, tmp, plan)
                rc, stderr = run_ffmpeg(cmd, duration)
                rescued = True
        if rc != 0:
            message, hint, _ = explain_error(stderr)
            log_path = write_log(args, src, cmd, stderr, "Conversion failed.")
            raise ConversionError(message, hint, "\n".join(last_lines(stderr, 4)) +
                                  (f"\nFull details: {log_path}" if log_path else ""))
        if not tmp.exists() or tmp.stat().st_size == 0:
            raise ConversionError("ffmpeg finished but produced no output file.",
                                  "Check free disk space and folder permissions.")

        if not args.no_verify:
            problems = verify_output(tools, tmp, plan, duration)
            for p in problems:
                say_warn(p)
        tmp.replace(dst)
    except KeyboardInterrupt:
        raise
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass

    notes = extract_sidecars(tools, args, src, dst, plan.sidecars) if plan.sidecars else []
    took = human_time(time.time() - started)
    msg = f"{dst.name}  ({human_size(size)} -> {human_size(dst.stat().st_size)}, {took})"
    if rescued:
        msg += "  [subtitles saved as separate files]"
    return "ok", msg


# --------------------------------------------------------------------------- #
#  Finding input files
# --------------------------------------------------------------------------- #
def collect_inputs(paths: List[str], recursive: bool):
    found: List[Tuple[Path, Path]] = []
    problems: List[Tuple[str, str, str]] = []
    seen = set()

    def add(p: Path, base: Path) -> None:
        key = p.resolve()
        if key not in seen:
            seen.add(key)
            found.append((p, base))

    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_file():
            add(p, p.parent)
        elif p.is_dir():
            walker = p.rglob("*") if recursive else p.glob("*")
            files = sorted((f for f in walker if f.is_file() and f.suffix.lower() in MKV_EXTENSIONS),
                           key=lambda f: str(f).lower())
            if not files:
                problems.append((raw, "No .mkv files found in this folder.",
                                 "Add -r to search sub-folders too." if not recursive else
                                 "Check you picked the right folder."))
            for f in files:
                add(f, p)
        elif any(ch in raw for ch in "*?["):
            matches = [Path(m) for m in sorted(glob.glob(raw, recursive=recursive)) if Path(m).is_file()]
            if not matches:
                problems.append((raw, "The pattern matched no files.", "Check the pattern and the folder."))
            for m in matches:
                add(m, m.parent)
        else:
            problems.append((raw, "Path not found.",
                             "Check the spelling, wrap paths containing spaces in quotes, and make sure "
                             "the drive is connected."))
    return found, problems


def output_path(src: Path, base: Path, args) -> Path:
    if args.output_dir:
        try:
            rel = src.parent.relative_to(base)
        except ValueError:
            rel = Path()
        return Path(args.output_dir).expanduser() / rel / (src.stem + ".mp4")
    return src.parent / (src.stem + ".mp4")


# --------------------------------------------------------------------------- #
#  Command line
# --------------------------------------------------------------------------- #
HELP_TEXT = """\
mkv2mp4 {version} - convert MKV files to MP4 without losing quality

USAGE
  python mkv2mp4.py <path> [<path> ...] [-r] [-o DIR] [--dry-run]
  python mkv2mp4.py -h

  <path> can be a file, a folder, or a wildcard in quotes.

OPTIONS
  -r, --recursive     Also search sub-folders. Only matters when <path> is a
                      folder (or a wildcard); without it only the top level of
                      the folder is scanned.

  -o DIR,             Write the MP4 files into DIR instead of next to each MKV.
  --output-dir DIR    DIR is created if it doesn't exist. With -r the folder
                      structure of the input is copied inside DIR.

  -n, --dry-run       Show the plan for every track (COPY / CONVERT / SAVE AS
                      FILE / SKIP) and the exact ffmpeg command, but write
                      nothing. Good for checking before a big batch.

  -h, --help, /?      Show this help and exit.
  --version           Show the version and exit.

EXAMPLES
  python mkv2mp4.py movie.mkv                  Convert one file; the MP4 appears
                                               next to it.
  python mkv2mp4.py D:\\Videos -r -o D:\\MP4s    Convert a whole folder tree and
                                               keep its folder structure.
  python mkv2mp4.py "*.mkv"                    Convert every MKV matching the
                                               wildcard.
  python mkv2mp4.py movie.mkv --dry-run        Only show the plan; change nothing.

WHAT IT DECIDES AUTOMATICALLY (there are no other options)
  Video      Copied untouched (no quality loss) if MP4 supports the codec
             (H.264, HEVC, AV1, VP9, MPEG-4). Otherwise re-encoded visually
             lossless (x264 CRF 14 / x265 CRF 16 for 10-bit).
  Audio      AAC and MP3 are copied untouched. Everything else (AC3, EAC3,
             DTS, TrueHD, FLAC, Opus...) is converted to high-bitrate AAC,
             because only AAC/MP3 play in web browsers, web players, phones
             and smart TVs. The channel count is always kept.
  Subtitles  Text subtitles are embedded as MP4 tracks. ASS/SSA files are also
             saved separately to keep their styling. Picture subtitles
             (PGS/VobSub) can't live in MP4, so they are saved as .sup/.mks
             files next to the MP4.
  Kept       Languages, track titles, default/forced flags and chapters.

SAFETY
  - Your original MKV is never modified or deleted.
  - An existing MP4 with the same name is never overwritten (it is skipped).
  - Output is written to NAME.partial.mp4 and renamed only when it succeeded.
  - The finished MP4 is re-checked (track count and duration).
  - Problem details are written to mkv2mp4.log in the current folder.

EXIT CODES
  0 everything OK    1 at least one file failed
  2 bad input or ffmpeg not found    130 stopped with Ctrl+C

REQUIREMENTS
  Python 3.8+ and ffmpeg/ffprobe (on PATH, or next to this script).
  See README.md for install steps and troubleshooting.
"""

# Everything else is decided automatically. These are the fixed "best" settings.
AUTO_SETTINGS = dict(
    overwrite=False,        # never replace an existing MP4 (it is skipped instead)
    audio_bitrate=None,     # AAC bitrate scales with channel count
    crf=None,               # 14 (x264) / 16 (x265) when a video track must be re-encoded
    preset="slow",
    no_verify=False,        # always double-check the finished MP4
    log=None,               # problems are written to ./mkv2mp4.log
    ffmpeg=None,            # found automatically (PATH, or next to this script)
    ffprobe=None,
    verbose=False,
)


def show_help() -> None:
    print(HELP_TEXT.format(version=__version__))


def parse_args(argv=None) -> argparse.Namespace:
    raw = list(sys.argv[1:] if argv is None else argv)
    if any(a in ("-h", "--help", "/?", "-?", "help") for a in raw):
        show_help()
        sys.exit(0)
    p = argparse.ArgumentParser(prog="mkv2mp4", add_help=False)
    p.add_argument("paths", nargs="*")
    p.add_argument("-r", "--recursive", action="store_true")
    p.add_argument("-o", "--output-dir")
    p.add_argument("-n", "--dry-run", action="store_true")
    p.add_argument("--version", action="version", version=f"mkv2mp4 {__version__}")
    p.error = lambda message: _usage_error(message)   # friendly message instead of argparse's
    args = p.parse_args(raw)
    for key, value in AUTO_SETTINGS.items():
        setattr(args, key, value)
    return args


def _usage_error(message: str) -> None:
    print(red(f"Error: {message}"), file=sys.stderr)
    print("Run  python mkv2mp4.py -h  to see the available options and examples.", file=sys.stderr)
    sys.exit(2)


# --------------------------------------------------------------------------- #
#  Main
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    global USE_COLOR
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    if os.name == "nt" and USE_COLOR:
        os.system("")  # enables ANSI colours on Windows 10+

    args = parse_args(argv)
    if not args.paths:
        print("No input given. Example: python mkv2mp4.py movie.mkv")
        print("Run  python mkv2mp4.py -h  to see all options and examples.")
        return 2

    try:
        tools = load_tools(args)
    except ConversionError as exc:
        say_fail(exc.message)
        if exc.hint:
            say_hint(exc.hint)
        return 2
    print(dim(f"Using {tools.version}"))

    inputs, problems = collect_inputs(args.paths, args.recursive)
    for raw, msg, hint in problems:
        say_fail(f"{raw}: {msg}")
        say_hint(hint)
    if not inputs:
        return 2

    results: List[Tuple[str, str, str]] = []   # (name, status, message)
    for raw, msg, _ in problems:
        results.append((raw, "failed", msg))
    claimed = set()
    code = 0
    try:
        for n, (src, base) in enumerate(inputs, 1):
            print()
            print(bold(f"[{n}/{len(inputs)}] {src.name}"))
            dst = output_path(src, base, args)
            key = str(dst).lower()
            try:
                if key in claimed:
                    raise ConversionError("Another input file would produce the same output name.",
                                          "Use -o with different folders, or rename one of the files.")
                claimed.add(key)
                status, msg = convert_one(tools, args, src, dst)
                if status == "ok":
                    print(green(f"  [OK] Done: {msg}"))
                elif status == "skipped":
                    print(yellow(f"  [-] Skipped: {msg}"))
                else:
                    print(cyan(f"  [i] {msg}"))
                results.append((src.name, status, msg))
            except ConversionError as exc:
                say_fail(exc.message)
                if exc.hint:
                    say_hint(exc.hint)
                if exc.details:
                    print(dim("      ffmpeg said:"))
                    for line in exc.details.splitlines():
                        print(dim(f"        {line}"))
                results.append((src.name, "failed", exc.message))
            except OSError as exc:
                say_fail(f"The operating system reported: {exc}")
                say_hint("Check that the output folder exists, is writable, and has free space.")
                results.append((src.name, "failed", str(exc)))
    except KeyboardInterrupt:
        print()
        say_warn("Stopped by you (Ctrl+C). The unfinished .partial.mp4 file was removed; finished files are kept.")
        return 130

    ok = [r for r in results if r[1] == "ok"]
    skipped = [r for r in results if r[1] == "skipped"]
    failed = [r for r in results if r[1] == "failed"]
    print()
    print(bold("Summary: ") + green(f"{len(ok)} converted") + ", " + yellow(f"{len(skipped)} skipped") + ", " +
          (red(f"{len(failed)} failed") if failed else "0 failed"))
    for name, _, msg in failed:
        print(red(f"   - {name}: {msg}"))
    if failed:
        code = 1
    return code

# =========================================================================== #
#  GUI  (tkinter is part of Python itself - nothing to install)
# =========================================================================== #
import queue
import tempfile
import traceback
import urllib.error
import urllib.request
import zipfile

try:
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import filedialog, messagebox, ttk
    HAVE_TK = True
except ImportError:                                   # pragma: no cover
    HAVE_TK = False

# Where "Download ffmpeg" gets it from (Windows). Tried in order.
FFMPEG_ZIP_URLS = [
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
]

# Dark theme (GitHub-style slate). Every colour in the window comes from here.
COL = dict(bg="#0d1117", card="#161b22", field="#0d1117", text="#e6edf3", muted="#8b949e", border="#30363d",
           accent="#2f6feb", accent_hover="#4c8dff", head="#010409",
           btn="#21262d", btn_hover="#30363d", btn_off="#161b22", text_off="#484f58", select="#1f3a5f",
           ok="#3fb950", warn="#e3b341", info="#79c0ff", fail="#ff7b72")
CHIP = {"ok": ("#12261a", "#3fb950"), "warn": ("#2d2208", "#e3b341"), "info": ("#0c2236", "#79c0ff"),
        "fail": ("#2d1214", "#ff7b72"), "none": ("#21262d", "#8b949e")}
ACTION_LABEL = {"copy": ("COPY", "ok"), "convert": ("CONVERT", "warn"), "reencode": ("RE-ENCODE", "warn"),
                "extract": ("SAVE AS FILE", "info"), "skip": ("SKIP", "fail")}
ACTION_HELP = {"copy": "copied untouched", "convert": "converted", "reencode": "re-encoded",
               "extract": "saved as separate file", "skip": "skipped"}


# ----------------------------- ffmpeg downloader --------------------------- #
def download_ffmpeg(progress, cancel: threading.Event) -> Path:
    """Download a Windows ffmpeg build and unpack ffmpeg.exe + ffprobe.exe into the per-user folder."""
    dest = user_bin_dir()
    dest.mkdir(parents=True, exist_ok=True)
    last_error = "unknown error"
    for url in FFMPEG_ZIP_URLS:
        tmp_name = None
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "mkv2mp4"})
            with urllib.request.urlopen(request, timeout=30) as response:
                total = int(response.headers.get("Content-Length") or 0)
                fd, tmp_name = tempfile.mkstemp(suffix=".zip")
                done = 0
                with os.fdopen(fd, "wb") as out:
                    while True:
                        if cancel.is_set():
                            raise Cancelled()
                        chunk = response.read(256 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                        done += len(chunk)
                        progress(done, total, url)
            wanted = {"ffmpeg.exe", "ffprobe.exe"}
            found = set()
            with zipfile.ZipFile(tmp_name) as archive:
                for member in archive.namelist():
                    base = member.rsplit("/", 1)[-1].lower()
                    if base in wanted and "/bin/" in member.lower():
                        with archive.open(member) as src, open(dest / base, "wb") as target:
                            shutil.copyfileobj(src, target)
                        found.add(base)
            if found != wanted:
                raise OSError("the downloaded archive did not contain ffmpeg.exe and ffprobe.exe")
            return dest
        except Cancelled:
            raise
        except (urllib.error.URLError, OSError, zipfile.BadZipFile, ValueError) as exc:
            last_error = f"{url}: {exc}"
        finally:
            if tmp_name and os.path.exists(tmp_name):
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
    raise ConversionError("ffmpeg could not be downloaded automatically.",
                          "Check your internet connection / firewall, or install it by hand:\n" + INSTALL_HINT,
                          last_error)


def open_path(path: Path) -> None:
    try:
        if os.name == "nt":
            os.startfile(str(path))                        # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError as exc:
        messagebox.showwarning("Can't open", f"Could not open:\n{path}\n\n{exc}")


def install_hooks(emit_fn, progress_fn, plan_fn) -> None:
    global EMIT, PROGRESS_CB, PLAN_CB
    EMIT, PROGRESS_CB, PLAN_CB = emit_fn, progress_fn, plan_fn


# ----------------------------------- App ----------------------------------- #
class DownloadDialog(tk.Toplevel):
    def __init__(self, parent, on_done):
        super().__init__(parent)
        self.title("Downloading ffmpeg")
        self.configure(bg=COL["card"], padx=20, pady=16)
        self.resizable(False, False)
        self.transient(parent)
        self.on_done = on_done
        self.cancel = threading.Event()
        self.q: "queue.Queue" = queue.Queue()
        tk.Label(self, text="Downloading ffmpeg (100-200 MB)...", bg=COL["card"], fg=COL["text"],
                 font=parent.f_bold).pack(anchor="w")
        self.msg = tk.Label(self, text="Starting...", bg=COL["card"], fg=COL["muted"], width=60, anchor="w")
        self.msg.pack(anchor="w", pady=(6, 6))
        self.bar = ttk.Progressbar(self, length=420, maximum=100)
        self.bar.pack(fill="x")
        self.btn = ttk.Button(self, text="Cancel", command=self._cancel)
        self.btn.pack(anchor="e", pady=(12, 0))
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.grab_set()
        threading.Thread(target=self._run, daemon=True).start()
        self.after(100, self._poll)

    def _cancel(self):
        self.cancel.set()
        self.msg.config(text="Cancelling...")

    def _run(self):
        def progress(done, total, url):
            self.q.put(("p", done, total, url))
        try:
            download_ffmpeg(progress, self.cancel)
            self.q.put(("done", None))
        except Cancelled:
            self.q.put(("cancelled", None))
        except ConversionError as exc:
            self.q.put(("error", exc))
        except Exception as exc:                          # noqa: BLE001
            self.q.put(("error", ConversionError(f"Unexpected problem: {exc}")))

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "p":
                    _, done, total, url = msg
                    host = url.split("/")[2]
                    if total:
                        self.bar.config(mode="determinate", value=done * 100 / total)
                        self.msg.config(text=f"{human_size(done)} of {human_size(total)}  from {host}")
                    else:
                        self.bar.config(mode="indeterminate")
                        self.bar.step(2)
                        self.msg.config(text=f"{human_size(done)}  from {host}")
                else:
                    self.grab_release()
                    self.destroy()
                    self.on_done(msg[0], msg[1])
                    return
        except queue.Empty:
            pass
        self.after(100, self._poll)


class App(tk.Tk):
    def __init__(self, initial_paths=None):
        super().__init__()
        self.title(f"MKV to MP4 Converter  -  v{__version__}")
        self.geometry("1120x940")
        self.minsize(1000, 760)
        self.configure(bg=COL["bg"])
        try:
            from ctypes import windll                     # sharper text on Windows high-DPI screens
            windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

        base = tkfont.nametofont("TkDefaultFont")
        family = base.actual()["family"]
        self.f_norm = (family, 10)
        self.f_bold = (family, 10, "bold")
        self.f_small = (family, 9)
        self.f_title = (family, 20, "bold")
        self.f_card = (family, 11, "bold")

        self.q: "queue.Queue" = queue.Queue()
        self.tools: Optional[Tools] = None
        self.items: List[dict] = []
        self.by_iid: dict = {}
        self.plan_cache: dict = {}
        self.analysing: set = set()
        self.running = False
        self.worker: Optional[threading.Thread] = None
        self.stopped = False
        self.last_output_dir: Optional[Path] = None
        self.file_started = 0.0
        self.total_jobs = 0
        self.done_jobs = 0
        self.log_path = user_bin_dir().parent / "mkv2mp4.log"

        self.out_mode = tk.StringVar(value="same")
        self.out_dir = tk.StringVar()
        self.recursive = tk.BooleanVar(value=False)
        self.dry = tk.BooleanVar(value=False)

        self._style()
        self._build()
        self._dark_title_bar()
        install_hooks(lambda k, t: self.q.put(("log", k, t)),
                      lambda cur, dur, spd: self.q.put(("progress", cur, dur, spd)),
                      lambda plan: self.q.put(("runplan", plan)))
        self.bind("<Delete>", lambda e: self.remove_selected())
        self.bind("<Control-o>", lambda e: self.add_files())
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._poll)
        threading.Thread(target=self._load_tools_bg, args=(self._args(False),), daemon=True).start()
        if initial_paths:
            self.after(200, lambda: self._add_paths(initial_paths, False))

    # ------------------------------ styling ------------------------------- #
    def _style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        c = COL
        st.configure(".", font=self.f_norm, background=c["card"], foreground=c["text"], bordercolor=c["border"],
                     lightcolor=c["card"], darkcolor=c["card"], troughcolor=c["field"], focuscolor=c["card"])
        for name, pad in (("TButton", (12, 6)), ("Small.TButton", (7, 6))):
            st.configure(name, padding=pad, background=c["btn"], foreground=c["text"], borderwidth=1,
                         bordercolor=c["border"], lightcolor=c["btn"], darkcolor=c["btn"])
            st.map(name, background=[("disabled", c["btn_off"]), ("active", c["btn_hover"])],
                   foreground=[("disabled", c["text_off"])], bordercolor=[("disabled", c["btn_off"])])
        st.configure("Accent.TButton", background=c["accent"], foreground="white", font=self.f_bold,
                     padding=(18, 9), bordercolor=c["accent"], lightcolor=c["accent"], darkcolor=c["accent"])
        st.map("Accent.TButton", background=[("disabled", "#1c2d4d"), ("active", c["accent_hover"])],
               foreground=[("disabled", "#6b7f9e")], bordercolor=[("disabled", "#1c2d4d")])
        st.configure("Stop.TButton", background="#3d1418", foreground=c["fail"], font=self.f_bold,
                     padding=(18, 9), bordercolor="#5a1d22", lightcolor="#3d1418", darkcolor="#3d1418")
        st.map("Stop.TButton", background=[("disabled", c["btn_off"]), ("active", "#5a1d22")],
               foreground=[("disabled", c["text_off"])], bordercolor=[("disabled", c["btn_off"])],
               lightcolor=[("disabled", c["btn_off"])], darkcolor=[("disabled", c["btn_off"])])
        for w in ("TCheckbutton", "TRadiobutton"):
            st.configure(w, background=c["card"], foreground=c["text"], indicatorbackground=c["field"],
                         indicatorforeground="white", upperbordercolor=c["border"], lowerbordercolor=c["border"])
            st.map(w, background=[("disabled", c["card"]), ("active", c["card"])],
                   foreground=[("disabled", c["text_off"])],
                   indicatorbackground=[("selected", c["accent"]), ("disabled", c["btn_off"])])
        st.configure("TEntry", fieldbackground=c["field"], foreground=c["text"], insertcolor=c["text"],
                     padding=5, bordercolor=c["border"], lightcolor=c["field"], darkcolor=c["field"])
        st.map("TEntry", fieldbackground=[("disabled", c["btn_off"])], foreground=[("disabled", c["text_off"])],
               bordercolor=[("focus", c["accent"])])
        st.configure("Treeview", background=c["field"], fieldbackground=c["field"], foreground=c["text"],
                     rowheight=27, borderwidth=1, bordercolor=c["border"], lightcolor=c["field"],
                     darkcolor=c["field"], font=self.f_norm)
        st.configure("Treeview.Heading", background="#1c2128", foreground=c["muted"], font=self.f_bold,
                     relief="flat", padding=(6, 6), bordercolor=c["border"], lightcolor="#1c2128", darkcolor="#1c2128")
        st.map("Treeview.Heading", background=[("active", c["btn_hover"])])
        st.map("Treeview", background=[("selected", c["select"])], foreground=[("selected", c["text"])])
        # work around a Tk 8.6.9 bug where Treeview row tag colours are ignored
        def fixed(option):
            return [e for e in st.map("Treeview", query_opt=option) if e[:2] != ("!disabled", "!selected")]
        st.map("Treeview", foreground=fixed("foreground"), background=fixed("background"))
        for orient in ("Vertical", "Horizontal"):
            st.configure(f"{orient}.TScrollbar", background="#30363d", troughcolor=c["field"], arrowcolor=c["muted"],
                         bordercolor=c["field"], lightcolor="#30363d", darkcolor="#30363d", gripcount=0, relief="flat")
            st.map(f"{orient}.TScrollbar", background=[("active", "#484f58"), ("pressed", "#6e7681")])
        for name, color in (("Horizontal.TProgressbar", c["accent"]), ("Ok.Horizontal.TProgressbar", c["ok"])):
            st.configure(name, troughcolor="#21262d", background=color, thickness=14, borderwidth=0,
                         bordercolor="#21262d", lightcolor=color, darkcolor=color)
        # the few classic Tk popups (right-click menus, combobox lists) follow the theme too
        self.option_add("*Menu.background", c["card"])
        self.option_add("*Menu.foreground", c["text"])
        self.option_add("*Menu.activeBackground", c["select"])
        self.option_add("*Menu.activeForeground", c["text"])

    def _dark_title_bar(self):
        """Windows 10/11: make the title bar dark too (silently skipped elsewhere)."""
        if os.name != "nt":
            return
        try:
            import ctypes
            self.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            value = ctypes.c_int(1)
            for attribute in (20, 19):          # 20 = Windows 11 / newer 10, 19 = older Windows 10
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value),
                                                              ctypes.sizeof(value)) == 0:
                    break
        except Exception:
            pass

    def _card(self, parent, number, title, **grid):
        outer = tk.Frame(parent, bg=COL["card"], highlightthickness=1, highlightbackground=COL["border"])
        outer.grid(**grid)
        head = tk.Frame(outer, bg=COL["card"])
        head.pack(fill="x", padx=14, pady=(12, 6))
        tk.Label(head, text=str(number), bg=COL["accent"], fg="white", width=2, font=self.f_bold).pack(side="left")
        tk.Label(head, text=f"  {title}", bg=COL["card"], fg=COL["text"], font=self.f_card).pack(side="left")
        body = tk.Frame(outer, bg=COL["card"])
        body.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        return outer, body, head

    # ------------------------------- layout ------------------------------- #
    def _build(self):
        self.columnconfigure(0, weight=5, uniform="c")
        self.columnconfigure(1, weight=6, uniform="c")
        self.rowconfigure(1, weight=2, minsize=300)
        self.rowconfigure(3, weight=1, minsize=190)

        # header
        header = tk.Frame(self, bg=COL["head"])
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        left = tk.Frame(header, bg=COL["head"])
        left.pack(side="left", padx=20, pady=14)
        tk.Label(left, text="MKV  to  MP4", bg=COL["head"], fg="white", font=self.f_title).pack(anchor="w")
        tk.Label(left, text="Lossless where possible: video copied, audio / subtitles / chapters kept.",
                 bg=COL["head"], fg=COL["muted"], font=self.f_norm).pack(anchor="w")
        right = tk.Frame(header, bg=COL["head"])
        right.pack(side="right", padx=20)
        self.ff_dot = tk.Label(right, text="\u25CF", bg=COL["head"], fg=COL["warn"], font=self.f_bold)
        self.ff_dot.pack(side="left")
        self.ff_text = tk.Label(right, text="Looking for ffmpeg...", bg=COL["head"], fg=COL["text"], font=self.f_norm)
        self.ff_text.pack(side="left", padx=(4, 10))
        self.ff_btn = ttk.Button(right, text="Download ffmpeg", command=self.get_ffmpeg)
        # (shown only when ffmpeg is missing)

        # left column: files + output
        leftcol = tk.Frame(self, bg=COL["bg"])
        leftcol.grid(row=1, column=0, sticky="nsew", padx=(14, 7), pady=(14, 7))
        leftcol.rowconfigure(0, weight=1)
        leftcol.columnconfigure(0, weight=1)

        files_card, files, _ = self._card(leftcol, 1, "Choose your videos", row=0, column=0, sticky="nsew")
        self.chk_recursive = ttk.Checkbutton(files, text="Also search sub-folders when adding a folder  (-r)",
                                             variable=self.recursive)
        self.chk_recursive.pack(side="bottom", anchor="w", pady=(10, 0))
        btns = tk.Frame(files, bg=COL["card"])
        btns.pack(side="bottom", fill="x", pady=(10, 0))
        self.btn_add_files = ttk.Button(btns, text="Add files...", style="Small.TButton", command=self.add_files)
        self.btn_add_folder = ttk.Button(btns, text="Add folder...", style="Small.TButton", command=self.add_folder)
        self.btn_remove = ttk.Button(btns, text="Remove", style="Small.TButton", command=self.remove_selected)
        self.btn_clear = ttk.Button(btns, text="Clear", style="Small.TButton", command=self.clear_files)
        for b in (self.btn_add_files, self.btn_add_folder, self.btn_remove, self.btn_clear):
            b.pack(side="left", padx=(0, 5))
        wrap = tk.Frame(files, bg=COL["card"])
        wrap.pack(side="top", fill="both", expand=True)
        self.file_tree = ttk.Treeview(wrap, columns=("name", "size", "status"), show="headings", selectmode="extended", height=5)
        for cid, text, width, anchor, stretch in (("name", "File", 190, "w", True), ("size", "Size", 70, "e", False),
                                                  ("status", "Status", 90, "w", False)):
            self.file_tree.heading(cid, text=text, anchor="w" if anchor == "w" else "e")
            self.file_tree.column(cid, width=width, anchor=anchor, stretch=stretch)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.file_tree.yview)
        self.file_tree.configure(yscrollcommand=sb.set)
        self.file_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for tag, color in (("done", COL["ok"]), ("failed", COL["fail"]), ("skipped", COL["warn"]),
                           ("working", COL["accent"]), ("stopped", COL["warn"])):
            self.file_tree.tag_configure(tag, foreground=color)
        self.file_tree.bind("<<TreeviewSelect>>", lambda e: self._on_select_file())
        self.empty_hint = tk.Label(wrap, text="No videos yet.\nClick \"Add files\" or \"Add folder\".\n\n"
                                   "Tip: drag .mkv files onto this\nprogram's icon to add them.",
                                   bg=COL["field"], fg=COL["muted"], font=self.f_small, justify="center")
        self.empty_hint.place(relx=0.5, rely=0.55, anchor="center")

        out_card, out, _ = self._card(leftcol, 2, "Where to save the MP4 files", row=1, column=0, sticky="ew", pady=(14, 0))
        self.rb_same = ttk.Radiobutton(out, text="Next to each original video", value="same",
                                       variable=self.out_mode, command=self._sync_output_state)
        self.rb_same.pack(anchor="w")
        row = tk.Frame(out, bg=COL["card"])
        row.pack(fill="x", pady=(6, 0))
        self.rb_folder = ttk.Radiobutton(row, text="This folder:", value="folder",
                                         variable=self.out_mode, command=self._sync_output_state)
        self.rb_folder.pack(side="left")
        self.out_entry = ttk.Entry(row, textvariable=self.out_dir)
        self.out_entry.pack(side="left", fill="x", expand=True, padx=8)
        self.btn_browse = ttk.Button(row, text="Browse...", command=self.browse_output)
        self.btn_browse.pack(side="left")
        tk.Label(out, text="Folder structure is kept when you add a folder with sub-folders.\n"
                 "An existing MP4 is never overwritten - it is skipped.", bg=COL["card"], fg=COL["muted"],
                 font=self.f_small, justify="left").pack(anchor="w", pady=(8, 0))
        self._sync_output_state()

        # right column: plan
        plan_card, plan, _ = self._card(self, 3, "What will happen to each track", row=1, column=1,
                                        sticky="nsew", padx=(7, 14), pady=(14, 7))
        self.plan_title = tk.Label(plan, text="", bg=COL["card"], fg=COL["text"], font=self.f_bold, anchor="w")
        self.plan_title.pack(fill="x")
        self.plan_status = tk.Label(plan, text="Add a video and select it to see the plan.", bg=COL["card"],
                                    fg=COL["muted"], anchor="w", justify="left", wraplength=560)
        self.plan_status.pack(fill="x", pady=(2, 6))
        self.chips = tk.Frame(plan, bg=COL["card"])
        self.chips.pack(fill="x", pady=(0, 8))
        self.plan_warn = tk.Label(plan, text="", bg=COL["card"], fg=COL["warn"], anchor="w", justify="left",
                                  wraplength=560)
        self.plan_warn.pack(side="bottom", fill="x", pady=(6, 0))
        self.plan_why = tk.Label(plan, text="Click a track to see why.", bg=COL["field"], fg=COL["text"],
                                 anchor="nw", justify="left", wraplength=560, padx=10, pady=8,
                                 highlightthickness=1, highlightbackground=COL["border"])
        self.plan_why.pack(side="bottom", fill="x", pady=(8, 0))
        pwrap = tk.Frame(plan, bg=COL["card"])
        pwrap.pack(side="top", fill="both", expand=True)
        self.plan_tree = ttk.Treeview(pwrap, columns=("idx", "type", "track", "action"), show="headings",
                                      selectmode="browse", height=6)
        for cid, text, width, stretch in (("idx", "#", 38, False), ("type", "Type", 78, False),
                                          ("track", "Track", 300, True), ("action", "Action", 130, False)):
            self.plan_tree.heading(cid, text=text, anchor="w")
            self.plan_tree.column(cid, width=width, anchor="w", stretch=stretch)
        psb = ttk.Scrollbar(pwrap, orient="vertical", command=self.plan_tree.yview)
        self.plan_tree.configure(yscrollcommand=psb.set)
        self.plan_tree.pack(side="left", fill="both", expand=True)
        psb.pack(side="right", fill="y")
        for key, (_, tone) in ACTION_LABEL.items():
            self.plan_tree.tag_configure(key, foreground=COL[tone])
        self.plan_tree.bind("<<TreeviewSelect>>", lambda e: self._on_select_track())
        self.plan_rows: dict = {}

        # convert card
        conv_card, conv, _ = self._card(self, 4, "Convert", row=2, column=0, columnspan=2, sticky="ew",
                                        padx=14, pady=(7, 7))
        top = tk.Frame(conv, bg=COL["card"])
        top.pack(fill="x")
        self.btn_start = ttk.Button(top, text="Start conversion", style="Accent.TButton", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(top, text="Stop", style="Stop.TButton", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=8)
        self.chk_dry = ttk.Checkbutton(top, text="Preview only - change nothing  (--dry-run)", variable=self.dry,
                                       command=self._sync_start_label)
        self.chk_dry.pack(side="left", padx=18)
        self.btn_open = ttk.Button(top, text="Open output folder", command=self.open_output, state="disabled")
        self.btn_open.pack(side="right")

        grid = tk.Frame(conv, bg=COL["card"])
        grid.pack(fill="x", pady=(12, 0))
        grid.columnconfigure(1, weight=1)
        tk.Label(grid, text="Current file", bg=COL["card"], fg=COL["muted"], width=12, anchor="w").grid(row=0, column=0)
        self.cur_bar = ttk.Progressbar(grid, maximum=100)
        self.cur_bar.grid(row=0, column=1, sticky="ew", padx=8)
        self.cur_text = tk.Label(grid, text="idle", bg=COL["card"], fg=COL["text"], width=34, anchor="w")
        self.cur_text.grid(row=0, column=2)
        tk.Label(grid, text="All files", bg=COL["card"], fg=COL["muted"], width=12, anchor="w").grid(row=1, column=0, pady=(8, 0))
        self.all_bar = ttk.Progressbar(grid, maximum=100, style="Ok.Horizontal.TProgressbar")
        self.all_bar.grid(row=1, column=1, sticky="ew", padx=8, pady=(8, 0))
        self.all_text = tk.Label(grid, text="0 of 0 finished", bg=COL["card"], fg=COL["text"], width=34, anchor="w")
        self.all_text.grid(row=1, column=2, pady=(8, 0))

        # log card
        log_card, log, loghead = self._card(self, 5, "Activity log", row=3, column=0, columnspan=2,
                                            sticky="nsew", padx=14, pady=(7, 14))
        for text, cmd in (("Open log file", self.open_log), ("Clear", self.clear_log), ("Copy", self.copy_log)):
            ttk.Button(loghead, text=text, command=cmd).pack(side="right", padx=(6, 0))
        logwrap = tk.Frame(log, bg=COL["head"], highlightthickness=1, highlightbackground=COL["border"])
        logwrap.pack(fill="both", expand=True)
        self.log = tk.Text(logwrap, height=7, wrap="word", font=("Consolas" if os.name == "nt" else "TkFixedFont", 9),
                           bg=COL["head"], fg=COL["text"], insertbackground=COL["text"], selectbackground="#264f78",
                           relief="flat", padx=8, pady=6, state="disabled", borderwidth=0, highlightthickness=0)
        logsb = ttk.Scrollbar(logwrap, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=logsb.set)
        logsb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        for tag, color in (("ok", "#56d364"), ("warn", "#e3b341"), ("fail", "#ff7b72"), ("hint", "#79c0ff"),
                           ("info", "#79c0ff"), ("skip", "#e3b341"), ("dim", "#8b949e"), ("head", "#ffffff")):
            self.log.tag_configure(tag, foreground=color)
        self.log.tag_configure("head", font=("TkDefaultFont", 10, "bold"))
        self._log("info", "Ready. Add some videos, check the plan, then press Start.")

    # ------------------------------ helpers ------------------------------- #
    def post(self, *msg):
        self.q.put(msg)

    def _log(self, kind, text):
        prefix = {"ok": "[OK] ", "warn": "[!] ", "fail": "[X] ", "hint": "    Hint: ", "info": "[i] ",
                  "skip": "[-] "}.get(kind, "")
        self.log.config(state="normal")
        self.log.insert("end", f"{prefix}{text}\n", kind)
        self.log.see("end")
        self.log.config(state="disabled")

    def clear_log(self):
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")

    def copy_log(self):
        self.clipboard_clear()
        self.clipboard_append(self.log.get("1.0", "end"))

    def open_log(self):
        if self.log_path.exists():
            open_path(self.log_path)
        else:
            messagebox.showinfo("No log yet", "The log file is only created when something goes wrong.")

    def _args(self, dry: bool):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        out = self.out_dir.get().strip() if self.out_mode.get() == "folder" else None
        ns = argparse.Namespace(paths=[], recursive=self.recursive.get(), output_dir=out or None, dry_run=dry)
        for key, value in AUTO_SETTINGS.items():
            setattr(ns, key, value)
        ns.log = str(self.log_path)
        return ns

    def _sync_output_state(self):
        state = "normal" if self.out_mode.get() == "folder" else "disabled"
        self.out_entry.config(state=state)
        self.btn_browse.config(state=state)

    def _sync_start_label(self):
        self.btn_start.config(text="Run preview" if self.dry.get() else "Start conversion")

    def browse_output(self):
        folder = filedialog.askdirectory(title="Choose where to save the MP4 files")
        if folder:
            self.out_dir.set(folder)
            self.out_mode.set("folder")
            self._sync_output_state()

    # --------------------------- ffmpeg handling -------------------------- #
    def _load_tools_bg(self, args):
        try:
            self.post("tools", load_tools(args), None)
        except ConversionError as exc:
            self.post("tools", None, exc)
        except Exception as exc:                           # noqa: BLE001
            self.post("tools", None, ConversionError(f"Unexpected problem while looking for ffmpeg: {exc}"))

    def _tools_ready(self, tools, error):
        self.tools = tools
        if tools:
            self.ff_dot.config(fg=COL["ok"])
            self.ff_text.config(text="ffmpeg ready")
            self.ff_btn.pack_forget()
            self._log("dim", f"Using {tools.version}")
            self._on_select_file()
        else:
            self.ff_dot.config(fg=COL["fail"])
            self.ff_text.config(text="ffmpeg not found")
            self.ff_btn.config(text="Download ffmpeg" if os.name == "nt" else "How to install ffmpeg")
            self.ff_btn.pack(side="left")
            self._log("fail", error.message if error else "ffmpeg not found")
            if error and error.hint:
                self._log("hint", error.hint)

    def get_ffmpeg(self):
        if os.name != "nt":
            messagebox.showinfo("Install ffmpeg", INSTALL_HINT)
            return
        if messagebox.askyesno("Download ffmpeg",
                               "ffmpeg is the free engine that does the actual conversion.\n\n"
                               "Download it now (100-200 MB) from the internet? It is stored in your user "
                               "profile only and needs no administrator rights."):
            DownloadDialog(self, self._download_finished)

    def _download_finished(self, kind, payload):
        if kind == "done":
            self._log("ok", "ffmpeg downloaded.")
            threading.Thread(target=self._load_tools_bg, args=(self._args(False),), daemon=True).start()
        elif kind == "error":
            self._log("fail", payload.message)
            messagebox.showerror("Download failed", f"{payload.message}\n\n{payload.hint or ''}\n\n{payload.details or ''}")
        else:
            self._log("warn", "Download cancelled.")

    # ------------------------------ file list ----------------------------- #
    def add_files(self):
        paths = filedialog.askopenfilenames(title="Choose MKV videos",
                                            filetypes=[("MKV videos", "*.mkv *.mk3d"), ("All files", "*.*")])
        if paths:
            self._add_paths(list(paths), False)

    def add_folder(self):
        folder = filedialog.askdirectory(title="Choose a folder with MKV videos")
        if folder:
            self._add_paths([folder], self.recursive.get())

    def _add_paths(self, paths, recursive):
        if self.running:
            return
        found, problems = collect_inputs([str(p) for p in paths], recursive)
        for raw, msg, hint in problems:
            self._log("fail", f"{raw}: {msg}")
            self._log("hint", hint)
        if problems and not found:
            messagebox.showinfo("Nothing to add", f"{problems[0][1]}\n\n{problems[0][2]}")
        known = {it["src"].resolve() for it in self.items}
        added = 0
        for src, base in found:
            if src.resolve() in known:
                continue
            try:
                size = human_size(src.stat().st_size)
            except OSError:
                size = "?"
            iid = self.file_tree.insert("", "end", values=(src.name, size, "Waiting"))
            item = {"src": src, "base": base, "iid": iid}
            self.items.append(item)
            self.by_iid[iid] = item
            added += 1
        if added:
            self._log("info", f"Added {added} file(s).")
            if not self.file_tree.selection():
                self.file_tree.selection_set(self.items[-added]["iid"])
        self._sync_empty()

    def remove_selected(self):
        if self.running:
            return
        for iid in self.file_tree.selection():
            item = self.by_iid.pop(iid, None)
            if item:
                self.items.remove(item)
                self.plan_cache.pop(item["src"], None)
            self.file_tree.delete(iid)
        self._sync_empty()
        if not self.items:
            self._clear_plan("Add a video and select it to see the plan.")

    def clear_files(self):
        if self.running:
            return
        for iid in list(self.by_iid):
            self.file_tree.delete(iid)
        self.items.clear()
        self.by_iid.clear()
        self.plan_cache.clear()
        self._sync_empty()
        self._clear_plan("Add a video and select it to see the plan.")

    def _sync_empty(self):
        if self.items:
            self.empty_hint.place_forget()
        else:
            self.empty_hint.place(relx=0.5, rely=0.55, anchor="center")

    def _set_status(self, item, text, tag=""):
        self.file_tree.set(item["iid"], "status", text)
        self.file_tree.item(item["iid"], tags=(tag,) if tag else ())

    # -------------------------------- plan view --------------------------- #
    def _clear_plan(self, message, title="", tone="muted", warn=""):
        self.plan_title.config(text=title)
        self.plan_status.config(text=message, fg=COL[tone] if tone in COL else COL["muted"])
        for child in self.chips.winfo_children():
            child.destroy()
        self.plan_tree.delete(*self.plan_tree.get_children())
        self.plan_rows = {}
        self.plan_why.config(text="Click a track to see why.")
        self.plan_warn.config(text=warn)

    def _on_select_file(self):
        sel = self.file_tree.selection()
        if not sel:
            return
        item = self.by_iid.get(sel[-1])
        if not item:
            return
        src = item["src"]
        cached = self.plan_cache.get(src)
        if cached:
            self._render_plan(src, *cached)
            return
        if self.tools is None:
            self._clear_plan("Waiting for ffmpeg... (see the top right corner)", title=src.name)
            return
        self._clear_plan("Reading the file...", title=src.name)
        if src not in self.analysing:
            self.analysing.add(src)
            threading.Thread(target=self._analyze_bg, args=(src, self._args(True)), daemon=True).start()

    def _analyze_bg(self, src, args):
        try:
            _, plan, _, _ = analyze(self.tools, args, src)
            self.post("analysis", src, plan, None)
        except ConversionError as exc:
            self.post("analysis", src, None, exc)
        except Exception as exc:                           # noqa: BLE001
            self.post("analysis", src, None, ConversionError(f"Unexpected problem: {exc}"))

    def _render_plan(self, src, plan, error):
        sel = self.file_tree.selection()
        current = self.by_iid.get(sel[-1])["src"] if sel and sel[-1] in self.by_iid else None
        if current != src:
            return
        if error is not None:
            hint = f"\nHint: {error.hint}" if error.hint else ""
            self._clear_plan(f"{error.message}{hint}", title=src.name, tone="fail")
            return
        self._clear_plan(f"{len(plan.items)} tracks found. Click a track to see why.", title=src.name)
        self._fill_chips(plan)
        for it in plan.items:
            s = it.stream
            label, _ = ACTION_LABEL[it.action]
            row = self.plan_tree.insert("", "end", values=(f"#{s.index}", s.kind.capitalize(), describe(s),
                                                           "\u25CF " + label), tags=(it.action,))
            extra = f"\n+ {it.extra}" if it.extra else ""
            self.plan_rows[row] = f"{label}: {it.reason}{extra}"
        if plan.warnings:
            self.plan_warn.config(text="\n".join("! " + w for w in plan.warnings))
        children = self.plan_tree.get_children()
        if children:
            self.plan_tree.selection_set(children[0])

    def _fill_chips(self, plan):
        def count(kind, *actions):
            return sum(1 for i in plan.items if i.stream.kind == kind and i.action in actions)

        def chip(title, text, tone):
            bg, fg = CHIP[tone]
            f = tk.Frame(self.chips, bg=bg, padx=10, pady=5)
            f.pack(side="left", padx=(0, 8))
            tk.Label(f, text=title, bg=bg, fg=fg, font=self.f_bold).pack(anchor="w")
            tk.Label(f, text=text, bg=bg, fg=fg, font=self.f_small).pack(anchor="w")

        vc, vr = count("video", "copy"), count("video", "reencode")
        chip("Video", ("re-encoded (visually lossless)" if vr else "copied untouched") if (vc or vr) else "none",
             "warn" if vr else "ok")
        ac, ao = count("audio", "copy"), count("audio", "convert")
        parts = ([f"{ac} copied"] if ac else []) + ([f"{ao} converted"] if ao else [])
        chip("Audio", ", ".join(parts) or "none", "none" if not parts else ("warn" if ao else "ok"))
        se, sx, ss = count("subtitle", "convert"), count("subtitle", "extract"), count("subtitle", "skip")
        parts = ([f"{se} embedded"] if se else []) + ([f"{sx} saved as files"] if sx else []) + \
                ([f"{ss} skipped"] if ss else [])
        chip("Subtitles", ", ".join(parts) or "none", "none" if not parts else ("info" if sx else "ok"))
        extras = sum(1 for i in plan.items if i.action == "skip" and i.stream.kind not in ("subtitle",))
        if extras:
            chip("Not kept", f"{extras} (fonts / attachments)", "fail")

    def _on_select_track(self):
        sel = self.plan_tree.selection()
        if sel and sel[0] in self.plan_rows:
            self.plan_why.config(text=self.plan_rows[sel[0]])

    # ------------------------------- converting --------------------------- #
    def _set_running(self, running):
        self.running = running
        for b in (self.btn_start, self.btn_add_files, self.btn_add_folder, self.btn_remove, self.btn_clear,
                  self.chk_recursive, self.chk_dry, self.rb_same, self.rb_folder):
            b.config(state="disabled" if running else "normal")
        self.btn_stop.config(state="normal" if running else "disabled")
        if running:
            self.out_entry.config(state="disabled")
            self.btn_browse.config(state="disabled")
        else:
            self._sync_output_state()

    def start(self):
        if self.running:
            return
        if not self.items:
            messagebox.showinfo("No videos", "Add at least one MKV file or folder first.")
            return
        if self.tools is None:
            messagebox.showwarning("ffmpeg missing", "ffmpeg was not found yet.\nUse the button in the top right "
                                   "corner to download or install it.")
            return
        if self.out_mode.get() == "folder" and not self.out_dir.get().strip():
            messagebox.showwarning("Choose a folder", "Pick the output folder, or switch to \"Next to each "
                                   "original video\".")
            return
        dry = self.dry.get()
        args = self._args(dry)
        jobs = list(self.items)
        for it in jobs:
            self._set_status(it, "Waiting")
        CANCEL.clear()
        self.stopped = False
        self.total_jobs, self.done_jobs = len(jobs), 0
        self.cur_bar.config(value=0, mode="determinate")
        self.all_bar.config(value=0)
        self.all_text.config(text=f"0 of {self.total_jobs} finished")
        self.btn_open.config(state="disabled")
        self._log("head", f"--- {'Preview' if dry else 'Conversion'} started: {len(jobs)} file(s) ---")
        self._set_running(True)
        self.worker = threading.Thread(target=self._work, args=(jobs, args), daemon=True)
        self.worker.start()

    def stop(self):
        if self.running:
            CANCEL.set()
            self.stopped = True
            self.btn_stop.config(state="disabled")
            self.cur_text.config(text="stopping...")
            self._log("warn", "Stopping after the current step...")

    def _work(self, jobs, args):
        counts = {"ok": 0, "skipped": 0, "failed": 0, "dryrun": 0, "stopped": 0}
        failed = []
        claimed = set()
        for n, item in enumerate(jobs, 1):
            if CANCEL.is_set():
                self.post("file_done", item, "stopped", "")
                counts["stopped"] += 1
                continue
            self.post("file_start", item, n)
            src = item["src"]
            dst = output_path(src, item["base"], args)
            try:
                key = str(dst).lower()
                if key in claimed:
                    raise ConversionError("Another input file would produce the same output name.",
                                          "Choose different folders or rename one of the files.")
                claimed.add(key)
                status, msg = convert_one(self.tools, args, src, dst)
                counts[status] += 1
                self.post("file_done", item, status, msg, str(dst.parent))
            except Cancelled:
                counts["stopped"] += 1
                self.post("file_done", item, "stopped", "")
            except ConversionError as exc:
                counts["failed"] += 1
                failed.append((src.name, exc.message))
                self.post("file_done", item, "failed", exc.message, "", exc)
            except OSError as exc:
                counts["failed"] += 1
                failed.append((src.name, str(exc)))
                err = ConversionError(f"The operating system reported: {exc}",
                                      "Check that the output folder exists, is writable and has free space.")
                self.post("file_done", item, "failed", err.message, "", err)
            except Exception as exc:                       # noqa: BLE001
                counts["failed"] += 1
                failed.append((src.name, str(exc)))
                err = ConversionError(f"Unexpected problem: {exc}", None, traceback.format_exc())
                self.post("file_done", item, "failed", err.message, "", err)
        self.post("finished", counts, failed)

    # ------------------------------- event pump --------------------------- #
    def _poll(self):
        try:
            while True:
                self._handle(self.q.get_nowait())
        except queue.Empty:
            pass
        self.after(80, self._poll)

    def _handle(self, msg):
        kind = msg[0]
        if kind == "log":
            self._log(msg[1], msg[2])
        elif kind == "tools":
            self._tools_ready(msg[1], msg[2])
        elif kind == "analysis":
            _, src, plan, error = msg
            self.analysing.discard(src)
            self.plan_cache[src] = (plan, error)
            self._render_plan(src, plan, error)
        elif kind == "runplan":
            # plan of the file being converted: show it and cache it
            item = getattr(self, "_current_item", None)
            if item:
                self.plan_cache[item["src"]] = (msg[1], None)
                self.file_tree.selection_set(item["iid"])
                self._render_plan(item["src"], msg[1], None)
        elif kind == "file_start":
            _, item, n = msg
            self._current_item = item
            self.file_started = time.time()
            self._set_status(item, "Working...", "working")
            self.file_tree.see(item["iid"])
            self.cur_bar.config(value=0)
            self.cur_text.config(text=item["src"].name[:34])
            self._log("head", f"[{n}/{self.total_jobs}] {item['src'].name}")
        elif kind == "progress":
            _, cur, dur, speed = msg
            if dur > 0:
                pct = max(0.0, min(100.0, cur / dur * 100))
                elapsed = time.time() - self.file_started
                eta = f"   ETA {human_time(elapsed * (100 - pct) / pct)}" if pct > 2 else ""
                self.cur_bar.config(mode="determinate", value=pct)
                self.cur_text.config(text=f"{pct:4.1f}%{eta}   {speed}")
                frac = (self.done_jobs + pct / 100) / max(1, self.total_jobs)
                self.all_bar.config(value=frac * 100)
            else:
                self.cur_text.config(text=f"{human_time(cur)} processed   {speed}")
        elif kind == "file_done":
            item, status, text = msg[1], msg[2], msg[3]
            self.done_jobs += 1
            labels = {"ok": ("Done", "done"), "skipped": ("Skipped", "skipped"), "failed": ("Failed", "failed"),
                      "dryrun": ("Previewed", "done"), "stopped": ("Stopped", "stopped")}
            label, tag = labels[status]
            self._set_status(item, label, tag)
            if status == "ok":
                self._log("ok", f"Done: {text}")
            elif status == "skipped":
                self._log("skip", f"Skipped: {text}")
            elif status == "dryrun":
                self._log("info", text)
            elif status == "failed":
                exc = msg[5] if len(msg) > 5 else None
                self._log("fail", text)
                if exc is not None:
                    if exc.hint:
                        self._log("hint", exc.hint)
                    if exc.details:
                        for line in exc.details.splitlines():
                            self._log("dim", f"    {line}")
            if len(msg) > 4 and msg[4]:
                self.last_output_dir = Path(msg[4])
            self.cur_bar.config(value=100 if status in ("ok", "skipped") else self.cur_bar["value"])
            self.all_bar.config(value=self.done_jobs * 100 / max(1, self.total_jobs))
            self.all_text.config(text=f"{self.done_jobs} of {self.total_jobs} finished")
        elif kind == "finished":
            self._finished(msg[1], msg[2])

    def _finished(self, counts, failed):
        self._set_running(False)
        self._current_item = None
        self.cur_text.config(text="idle")
        self.cur_bar.config(value=0)
        if self.last_output_dir or self.out_mode.get() == "folder":
            self.btn_open.config(state="normal")
        summary = (f"{counts['ok']} converted, {counts['skipped']} skipped, {counts['failed']} failed" +
                   (f", {counts['dryrun']} previewed" if counts["dryrun"] else "") +
                   (f", {counts['stopped']} stopped" if counts["stopped"] else ""))
        self._log("head", f"Summary: {summary}")
        for name, why in failed:
            self._log("fail", f"{name}: {why}")
        text = summary
        if failed:
            text += "\n\nFailed:\n" + "\n".join(f"  - {n}: {w}" for n, w in failed[:8])
            text += "\n\nDetails are in the log below (and in mkv2mp4.log)."
        if counts["ok"] and not self.dry.get() and not self.stopped:
            if messagebox.askyesno("Finished", text + "\n\nOpen the output folder?"):
                self.open_output()
        elif failed:
            messagebox.showwarning("Finished with problems", text)
        else:
            messagebox.showinfo("Finished", text)

    def open_output(self):
        folder = Path(self.out_dir.get().strip()) if self.out_mode.get() == "folder" and self.out_dir.get().strip() \
            else self.last_output_dir
        if folder and folder.exists():
            open_path(folder)
        else:
            messagebox.showinfo("Nothing to open", "No output folder yet.")

    def _on_close(self):
        if self.running:
            if not messagebox.askyesno("Stop and exit?", "A conversion is running. Stop it and exit?\n"
                                       "The unfinished file will be removed."):
                return
            CANCEL.set()
            if self.worker:
                self.worker.join(timeout=5)
        self.destroy()

    def report_callback_exception(self, exc, val, tb):    # keep a windowed app from dying silently
        text = "".join(traceback.format_exception(exc, val, tb))
        try:
            self._log("fail", f"Internal error: {val}")
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(f"\n===== {dt.datetime.now():%Y-%m-%d %H:%M:%S} internal error\n{text}\n")
        except Exception:
            pass
        messagebox.showerror("Unexpected error", f"Something went wrong: {val}\n\nDetails were saved to:\n{self.log_path}")


def main_gui(argv=None) -> int:
    if not HAVE_TK:
        msg = ("The graphical interface needs tkinter, which comes with Python.\n"
               "Windows/macOS: re-run the Python installer and tick 'tcl/tk and IDLE'.\n"
               "Ubuntu/Debian: sudo apt install python3-tk")
        print(msg)
        return 2
    paths = [a for a in (sys.argv[1:] if argv is None else argv) if not a.startswith("-")]
    App(paths).mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main_gui())
