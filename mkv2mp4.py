#!/usr/bin/env python3
"""
mkv2mp4 - convert MKV files to MP4 with ffmpeg, keeping quality and tracks.

Usage (that's all - it picks the best settings itself and starts right away):
    python mkv2mp4.py movie.mkv                  convert one file (MP4 appears next to it)
    python mkv2mp4.py D:\\Videos -r -o D:\\MP4s    whole folder tree, keep the folder structure
    python mkv2mp4.py "*.mkv"                    wildcard
    python mkv2mp4.py movie.mkv --dry-run        only show the plan, change nothing

What it decides automatically, per stream:
    video     copied untouched (lossless) when MP4 can hold the codec, else re-encoded visually lossless
    audio     AAC/MP3 copied untouched; everything else -> high-bitrate AAC (plays in browsers and web players)
    subtitles text subs embedded (ASS originals also saved as .ass); picture subs (PGS/VobSub)
              saved as side files because MP4 cannot hold them
Languages, track names, default/forced flags and chapters are kept. Existing MP4s are never overwritten.
Needs Python 3.8+ and ffmpeg/ffprobe. No third-party Python packages.
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


if __name__ == "__main__":
    sys.exit(main())
