#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BananaLinux Flasher
===================

A minimalist, cross-platform (Linux / Windows / macOS) GUI utility that:

  1. Dynamically resolves the newest BananaLinux release on GitHub and picks the
     correct image asset for the selected Anbernic handheld.
  2. Enumerates physical microSD drives, always filtering out the drive the
     host operating system is running from.
  3. Securely wipes the partition table / boot area of the target (wipefs + dd
     on Linux, diskpart on Windows, diskutil on macOS).
  4. Decompresses the ``.img.gz`` on the fly and streams the raw bytes straight
     to the block device -- no BalenaEtcher, Rufus or other third-party flasher.

Only the Python standard library is required (Tkinter ships with CPython).

Running it
----------
    Linux / macOS :  python3 flash_banana.py      # will ask for your sudo password
    Windows       :  python flash_banana.py       # run as Administrator (auto-elevates)

Author: built for Thomas' BananaLinux flashing workflow.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
import os
import plistlib
import queue
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

import tkinter as tk
from tkinter import scrolledtext, ttk

# ---------------------------------------------------------------------------
#  Platform detection
# ---------------------------------------------------------------------------
IS_WINDOWS = sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

DEVNULL = subprocess.DEVNULL

# ---------------------------------------------------------------------------
#  Application / release constants
# ---------------------------------------------------------------------------
APP_NAME = "BananaLinux Flasher"
APP_VERSION = "1.0.0"

GITHUB_REPO = "JorreFog/BananaLinux"
RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASES_LIST_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases"
RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"

USER_AGENT = f"{APP_NAME}/{APP_VERSION} (+https://github.com/{GITHUB_REPO})"

# Friendly model name -> release asset filename
MODELS = {
    "Anbernic RG DS Plus": "banana-rgds-plus.img.gz",
    "Anbernic RG DS": "banana-rgds.img.gz",
}
DEFAULT_MODEL = "Anbernic RG DS Plus"

CHUNK_SIZE = 1 << 20          # 1 MiB streaming granularity
ZERO_WIPE_BYTES = 10 * 1024 * 1024   # "first 10MB" zeroing on Linux

# sudo is resolved once at runtime; an empty list means "we are already root".
SUDO_PREFIX: list[str] = []


# ===========================================================================
#  Small helpers
# ===========================================================================
def format_bytes(num: float) -> str:
    """Human friendly byte size."""
    if not num or num < 0:
        return "?"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if num < 1024.0 or unit == "TiB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024.0
    return f"{num:.1f} TiB"


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}h {minutes:02d}m"
    if minutes:
        return f"{minutes:d}m {secs:02d}s"
    return f"{secs:d}s"


def is_elevated() -> bool:
    """True when the process can already write raw block devices."""
    if IS_WINDOWS:
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    try:
        return os.geteuid() == 0  # type: ignore[attr-defined]
    except Exception:
        return False


class Cancelled(Exception):
    """Raised internally when the user presses Cancel."""


class FlasherError(RuntimeError):
    """A user-facing error with a clean message."""


# ===========================================================================
#  Privilege handling (sudo caching + keep-alive, Windows elevation)
# ===========================================================================
class Privileges:
    """
    Manages elevated access in a portable way.

    * Linux / macOS : validates the sudo password once, caches the sudo
      timestamp and keeps it alive in a daemon thread, so the actual flashing
      commands can run non-interactively (`sudo -n`) while stdin stays free for
      the image data.
    * Windows       : the process must already be elevated; otherwise we offer
      to relaunch through the UAC prompt.
    """

    def __init__(self, log):
        self._log = log
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- Windows -----------------------------------------------------------
    @staticmethod
    def relaunch_as_admin() -> bool:
        """Re-launch the current script with the UAC 'runas' verb."""
        try:
            import ctypes

            params = " ".join(f'"{arg}"' for arg in sys.argv)
            rc = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", sys.executable, params, None, 1
            )
            return int(rc) > 32
        except Exception as exc:  # pragma: no cover - Windows only
            print(f"Could not elevate: {exc}", file=sys.stderr)
            return False

    # -- POSIX -------------------------------------------------------------
    def cache_sudo(self, password: str) -> bool:
        """Validate `password` and remember it in sudo's timestamp file."""
        global SUDO_PREFIX
        if is_elevated():
            SUDO_PREFIX = []
            return True
        try:
            proc = subprocess.run(
                ["sudo", "-S", "-p", "", "-v"],
                input=(password + "\n").encode(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self._log(f"sudo failed: {exc}")
            return False

        if proc.returncode != 0:
            detail = proc.stderr.decode(errors="replace").strip()
            self._log(f"sudo rejected the password. {detail}")
            return False

        SUDO_PREFIX = ["sudo", "-n"]
        self._start_keepalive()
        return True

    def _start_keepalive(self) -> None:
        """Refresh the sudo timestamp so long flashes never time out."""
        if self._thread is not None:
            return

        def loop() -> None:
            # Default sudo timeout is 5 min; refresh comfortably inside that.
            while not self._stop.wait(240):
                try:
                    subprocess.run(
                        ["sudo", "-n", "-v"],
                        stdout=DEVNULL,
                        stderr=DEVNULL,
                        timeout=15,
                    )
                except Exception:
                    pass

        self._thread = threading.Thread(
            target=loop, name="sudo-keepalive", daemon=True
        )
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()


# ===========================================================================
#  Networking -- GitHub release lookup + streaming download
# ===========================================================================
def _open_url(url: str, timeout: int = 30):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
        },
    )
    return urllib.request.urlopen(request, timeout=timeout)


def _fetch_json(url: str) -> object:
    """GET `url` and parse the JSON body, mapping errors to FlasherError."""
    try:
        with _open_url(url) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise FlasherError(f"GitHub returned HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise FlasherError(
            f"Could not reach GitHub ({exc.reason}). Check your connection."
        ) from exc
    except Exception as exc:  # malformed JSON, TLS issues, ...
        raise FlasherError(f"Unexpected error talking to GitHub: {exc}") from exc


def _release_candidates() -> list[dict]:
    """
    Return candidate release objects, newest first.

    The documented `/releases/latest` endpoint is tried first, exactly as
    requested -- but the BananaLinux project currently ships **only
    pre-releases**, and GitHub's `latest` endpoint deliberately ignores
    pre-releases (returning 404). We therefore fall back to the release list
    and walk it newest-first, skipping drafts, so the tool keeps working.
    """
    errors: list[FlasherError] = []
    releases: list[dict] = []
    seen: set = set()

    def add(release: dict) -> None:
        key = release.get("id") or release.get("tag_name")
        if key in seen:
            return
        seen.add(key)
        releases.append(release)

    try:
        latest = _fetch_json(RELEASES_API)
        if isinstance(latest, dict):
            add(latest)
    except FlasherError as exc:
        errors.append(exc)

    try:
        listing = _fetch_json(f"{RELEASES_LIST_API}?per_page=30")
        if isinstance(listing, list):
            for release in listing:
                if isinstance(release, dict) and not release.get("draft"):
                    add(release)
    except FlasherError as exc:
        errors.append(exc)

    if not releases and errors:
        # Surface the real reason (offline, rate-limited, ...) instead of a
        # misleading "no release found".
        raise errors[0]
    return releases


def resolve_release_asset(asset_name: str) -> dict:
    """
    Return {'name', 'url', 'size', 'tag', 'checksum'} for `asset_name`.

    The newest non-draft release that actually contains the requested asset
    wins. `checksum` is the matching SHA-256 from the release's SHA256SUMS
    file when one is published, otherwise None.
    """
    seen_failure: str | None = None
    tried: list[str] = []

    for release in _release_candidates():
        assets = {
            a.get("name"): a for a in (release.get("assets") or []) if a.get("name")
        }
        if not assets:
            continue

        tag = release.get("tag_name") or "latest"
        asset = assets.get(asset_name)
        if asset is None:
            tried.append(tag)
            continue

        # Model tokens let us pair e.g. banana-rgds-plus.img.gz with
        # SHA256SUMS-rgds-plus.txt (but not with SHA256SUMS-rgds.txt).
        want = _model_tokens(asset_name)
        sums_files = [
            name for name in assets
            if name.startswith("SHA256SUMS") and name.endswith(".txt")
        ]
        sums_files.sort(key=lambda name: _model_tokens(name) != want)

        checksum = None
        for sums_name in sums_files:
            digest = _fetch_checksum(assets[sums_name]["browser_download_url"],
                                     asset_name)
            if digest:
                checksum = digest
                break

        return {
            "name": asset["name"],
            "url": asset["browser_download_url"],
            "size": int(asset.get("size") or 0),
            "tag": tag,
            "checksum": checksum,
        }

    if tried:
        seen_failure = (
            f"'{asset_name}' was not found in these recent releases: "
            + ", ".join(tried[:6])
        )
    raise FlasherError(
        seen_failure
        or f"No published release with assets was found for {GITHUB_REPO}. "
           "See " + RELEASES_PAGE
    )


def _model_tokens(name: str) -> list[str]:
    """Reduce an asset/sums filename to its model tokens, e.g. ['rgds','plus']."""
    stem = name
    for suffix in (".img.gz", ".update.tar", ".img", ".txt", ".tar"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    tokens = [t for t in stem.split("-") if t]
    for prefix in ("banana", "SHA256SUMS"):
        if tokens and tokens[0] == prefix:
            tokens = tokens[1:]
    return tokens


def _fetch_checksum(sums_url: str, asset_name: str) -> str | None:
    """
    Fetch a SHA256SUMS file and return the hex digest whose *exact* filename
    matches `asset_name`, or None when the file does not cover that asset.
    """
    try:
        with _open_url(sums_url) as response:
            text = response.read().decode("utf-8", errors="replace")
    except Exception:
        return None

    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        digest = parts[0].strip()
        filename = parts[-1].lstrip("*").strip()
        if filename != asset_name:
            continue
        if len(digest) == 64 and all(c in "0123456789abcdefABCDEF" for c in digest):
            return digest.lower()
    return None


def download_asset(
    url: str,
    destination: str,
    progress,
    cancel: threading.Event,
) -> None:
    """Stream `url` to `destination`, reporting (done_bytes, total_bytes)."""
    try:
        response = _open_url(url)
    except urllib.error.HTTPError as exc:
        raise FlasherError(f"Download failed with HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise FlasherError(f"Download failed: {exc.reason}") from exc

    with response:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        progress(0, total)
        with open(destination, "wb") as handle:
            while True:
                if cancel.is_set():
                    raise Cancelled()
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                handle.write(chunk)
                done += len(chunk)
                progress(done, total)

    if total and done != total:
        raise FlasherError(
            f"Download was truncated ({format_bytes(done)} of "
            f"{format_bytes(total)})."
        )


def sha256_file(path: str, cancel: threading.Event | None = None) -> str:
    """Stream a file through SHA-256, honouring cancellation."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            chunk = handle.read(4 << 20)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def gzip_uncompressed_size(path: str) -> int:
    """
    Read the ISIZE trailer of a gzip stream for an approximate progress total.

    The value is stored modulo 2**32, so images larger than 4 GiB report a
    wrapped number; callers treat an implausible value as "unknown".
    """
    try:
        size = os.path.getsize(path)
        if size < 18:
            return 0
        with open(path, "rb") as handle:
            handle.seek(-4, os.SEEK_END)
            trailer = handle.read(4)
        return struct.unpack("<I", trailer)[0]
    except OSError:
        return 0


# ===========================================================================
#  Device enumeration -- every platform, OS drive always excluded
# ===========================================================================
def _device(path, raw, label, size, removable, protected, detail="") -> dict:
    return {
        "path": path,       # addressable device node / handle
        "raw": raw,         # raw (unbuffered) variant used for flashing
        "label": label,     # text shown in the dropdown
        "size": int(size or 0),
        "removable": bool(removable),
        "protected": bool(protected),
        "detail": detail,
        "index": None,      # Windows physical drive number
    }


def list_devices() -> list[dict]:
    """Return the physical drives of this machine, OS drive marked protected."""
    if IS_WINDOWS:
        return _list_devices_windows()
    if IS_MACOS:
        return _list_devices_macos()
    return _list_devices_linux()


# -- Linux ------------------------------------------------------------------
def _linux_root_disk() -> str | None:
    """Best-effort identification of the disk backing '/'."""
    for cmd in (
        ["findmnt", "-no", "SOURCE", "/"],
        ["sh", "-c", "grep -E ' / ' /proc/mounts | head -n1 | cut -d' ' -f1"],
    ):
        try:
            out = subprocess.check_output(cmd, text=True, stderr=DEVNULL).strip()
        except Exception:
            continue
        if not out.startswith("/dev/"):
            continue
        node = out.split(" ")[0]
        base = os.path.realpath(node)
        # Strip a partition suffix: sda2 -> sda, nvme0n1p2 -> nvme0n1, mmcblk0p1 -> mmcblk0
        name = os.path.basename(base)
        try:
            sysname = subprocess.check_output(
                ["lsblk", "-no", "PKNAME", base], text=True, stderr=DEVNULL
            ).strip().splitlines()
            if sysname and sysname[0]:
                return sysname[0]
        except Exception:
            pass
        stripped = name
        if "nvme" in name or "mmcblk" in name:
            stripped = name.split("p")[0]
        return stripped
    return None


def _list_devices_linux() -> list[dict]:
    devices: list[dict] = []
    root_disk = _linux_root_disk()

    # Preferred path: lsblk gives us model names and mountpoints in one shot.
    try:
        out = subprocess.check_output(
            [
                "lsblk", "-d", "-b", "-J",
                "-o", "NAME,SIZE,MODEL,TRAN,RM,TYPE,MOUNTPOINTS",
            ],
            text=True,
            stderr=DEVNULL,
        )
        data = json.loads(out)
        for block in data.get("blockdevices", []):
            if (block.get("type") or "").lower() != "disk":
                continue
            name = block.get("name") or ""
            if not name or name.startswith(("loop", "ram", "zram", "dm-")):
                continue
            size = int(block.get("size") or 0)
            model = (block.get("model") or "").strip()
            tran = (block.get("tran") or "").strip().upper()
            removable = block.get("rm") is True or block.get("rm") == "1"
            mounts = block.get("mountpoints") or []
            has_mount = any(mounts)
            protected = (name == root_disk) or has_mount

            label = f"/dev/{name}"
            if size:
                label += f"  —  {format_bytes(size)}"
            if model:
                label += f"  ({model})"
            if removable or tran == "USB":
                label += "  [USB/SD]"
            if protected:
                label += "  ⚠ SYSTEM / MOUNTED"

            device = _device(
                path=f"/dev/{name}",
                raw=f"/dev/{name}",
                label=label,
                size=size,
                removable=removable or tran == "USB",
                protected=protected,
                detail=(f"{model} {tran}").strip(),
            )
            devices.append(device)
    except Exception:
        # Fallback: pure /sys/block probing (works on minimal systems).
        try:
            names = sorted(os.listdir("/sys/block"))
        except OSError:
            return []

        for name in names:
            if name.startswith(("loop", "ram", "zram", "dm-", "sr", "md")):
                continue
            base = os.path.join("/sys/block", name)
            try:
                with open(os.path.join(base, "size")) as fh:
                    size = int(fh.read().strip()) * 512
            except Exception:
                size = 0
            removable = False
            try:
                with open(os.path.join(base, "removable")) as fh:
                    removable = fh.read().strip() == "1"
            except Exception:
                pass
            model = ""
            try:
                with open(os.path.join(base, "device", "model")) as fh:
                    model = fh.read().strip()
            except Exception:
                pass
            protected = name == root_disk

            label = f"/dev/{name}"
            if size:
                label += f"  —  {format_bytes(size)}"
            if model:
                label += f"  ({model})"
            if removable:
                label += "  [Removable]"
            if protected:
                label += "  ⚠ SYSTEM"

            devices.append(
                _device(
                    path=f"/dev/{name}",
                    raw=f"/dev/{name}",
                    label=label,
                    size=size,
                    removable=removable,
                    protected=protected,
                    detail=model,
                )
            )

    # Removable / external first, protected last.
    devices.sort(key=lambda d: (d["protected"], not d["removable"], -(d["size"] or 0)))
    return devices


# -- macOS ------------------------------------------------------------------
def _list_devices_macos() -> list[dict]:
    devices: list[dict] = []
    try:
        raw = subprocess.check_output(
            ["diskutil", "list", "-plist"], stderr=DEVNULL
        )
        listing = plistlib.loads(raw)
    except Exception:
        return []

    for disk in listing.get("WholeDisks", []):
        try:
            info_raw = subprocess.check_output(
                ["diskutil", "info", "-plist", disk], stderr=DEVNULL
            )
            info = plistlib.loads(info_raw)
        except Exception:
            continue

        node = info.get("DeviceNode") or f"/dev/{disk}"
        size = int(info.get("Size") or info.get("TotalSize") or 0)
        removable = bool(info.get("RemovableMedia") or info.get("Removable"))
        internal = bool(info.get("Internal"))
        mounted = bool(info.get("Mounted"))
        name = info.get("MediaName") or info.get("DeviceNode") or disk

        # Never allow the internal boot disk.
        protected = internal and not removable

        label = f"{node}  —  {format_bytes(size)}"
        if name:
            label += f"  ({name})"
        if removable:
            label += "  [Removable]"
        if mounted:
            label += "  [mounted]"
        if protected:
            label += "  ⚠ INTERNAL / SYSTEM"

        devices.append(
            _device(
                path=node,
                raw=node.replace("/dev/disk", "/dev/rdisk"),
                label=label,
                size=size,
                removable=removable,
                protected=protected,
                detail=str(name),
            )
        )

    devices.sort(key=lambda d: (d["protected"], not d["removable"], -(d["size"] or 0)))
    return devices


# -- Windows ----------------------------------------------------------------
def _windows_system_disk_number() -> int | None:
    """Disk number that currently holds the running Windows installation."""
    script = (
        "$d = Get-Partition -DriveLetter ($env:SystemDrive[0]) -ErrorAction Stop;"
        "$d.DiskNumber"
    )
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            text=True,
            stderr=DEVNULL,
            timeout=30,
        ).strip()
        return int(out.splitlines()[0])
    except Exception:
        return 0  # Conservative fallback: disk 0 is almost always the OS disk.


def _list_devices_windows() -> list[dict]:
    devices: list[dict] = []
    system_disk = _windows_system_disk_number()

    script = (
        "Get-CimInstance Win32_DiskDrive | "
        "Select-Object Index,Model,Size,InterfaceType,MediaType | "
        "ConvertTo-Json -Compress"
    )
    records: list[dict] = []
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            text=True,
            stderr=DEVNULL,
            timeout=45,
        ).strip()
        if out:
            parsed = json.loads(out)
            records = parsed if isinstance(parsed, list) else [parsed]
    except Exception:
        records = []

    if not records:
        # Blind fallback: probe the 16 conventional physical drive handles.
        for index in range(16):
            devices.append(
                _device(
                    path=rf"\\.\PhysicalDrive{index}",
                    raw=rf"\\.\PhysicalDrive{index}",
                    label=rf"\\.\PhysicalDrive{index}  (device handle)",
                    size=0,
                    removable=False,
                    protected=(index == system_disk),
                )
            )
        return devices

    for rec in records:
        index = int(rec.get("Index") or 0)
        size = int(rec.get("Size") or 0)
        model = (rec.get("Model") or "").strip()
        interface = (rec.get("InterfaceType") or "").strip()
        media = (rec.get("MediaType") or "").strip()
        removable = ("removable" in media.lower()) or (interface.upper() == "USB")
        protected = index == system_disk

        label = rf"\\.\PhysicalDrive{index}"
        if size:
            label += f"  —  {format_bytes(size)}"
        if model:
            label += f"  ({model})"
        if interface:
            label += f"  [{interface}]"
        if protected:
            label += "  ⚠ SYSTEM DISK"

        device = _device(
            path=rf"\\.\PhysicalDrive{index}",
            raw=rf"\\.\PhysicalDrive{index}",
            label=label,
            size=size,
            removable=removable,
            protected=protected,
            detail=f"{model} {interface}".strip(),
        )
        device["index"] = index
        devices.append(device)

    devices.sort(key=lambda d: (d["protected"], not d["removable"], -(d["size"] or 0)))
    return devices


# ===========================================================================
#  Secure wipe (partition table / first sectors)
# ===========================================================================
def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run `cmd` with the active sudo prefix, raising FlasherError on failure."""
    full = list(SUDO_PREFIX) + list(cmd)
    try:
        return subprocess.run(full, check=True, **kwargs)
    except FileNotFoundError as exc:
        raise FlasherError(f"Required tool not found: {cmd[0]}") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b"").decode(errors="replace").strip()
        raise FlasherError(
            f"Command failed ({cmd[0]}, exit {exc.returncode})."
            + (f"\n{stderr}" if stderr else "")
        ) from exc


def wipe_device(device: dict, log) -> None:
    """Erase the partition table and leading sectors of `device`."""
    if IS_WINDOWS:
        _wipe_windows(device, log)
    elif IS_MACOS:
        _wipe_macos(device, log)
    else:
        _wipe_linux(device, log)


def _wipe_linux(device: dict, log) -> None:
    path = device["path"]
    log(f"Unmounting any volumes on {path} ...")
    # Any mounted partition must be released before we clobber the disk.
    try:
        mounts = subprocess.check_output(["lsblk", "-no", "MOUNTPOINT", path],
                                         text=True, stderr=DEVNULL)
    except Exception:
        mounts = ""
    for point in (m.strip() for m in mounts.splitlines()):
        if not point:
            continue
        log(f"  unmounting {point}")
        try:
            _run(["umount", point], stdout=DEVNULL, stderr=DEVNULL)
        except FlasherError as exc:
            log(f"  could not unmount {point}: {exc}")

    log(f"Wiping filesystem signatures on {path} (wipefs) ...")
    try:
        _run(["wipefs", "-a", path],
             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except FlasherError as exc:
        # wipefs is optional -- zeroing below still destroys the partition table.
        log(f"  wipefs skipped: {exc}")

    log(f"Zeroing the first {format_bytes(ZERO_WIPE_BYTES)} of {path} ...")
    _run([
        "dd", "if=/dev/zero", f"of={path}",
        "bs=1M", f"count={ZERO_WIPE_BYTES // (1024 * 1024)}",
        "conv=fsync", "status=none",
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    _run(["sync"], stdout=DEVNULL, stderr=DEVNULL)
    log("  Target wiped.")


def _wipe_macos(device: dict, log) -> None:
    path = device["path"]
    log(f"Unmounting {path} ...")
    _run(["diskutil", "unmountDisk", "force", path],
         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    log(f"Erasing partition map on {path} (diskutil) ...")
    try:
        _run(["diskutil", "eraseDisk", "free", "%noformat%", path],
             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except FlasherError as exc:
        # Erase can fail on some media/systems; the raw streaming write below
        # still overwrites the whole partition table, so this is non-fatal.
        log(f"  eraseDisk skipped: {exc}")
    log("  Target wiped.")


def _wipe_windows(device: dict, log) -> None:
    index = device.get("index")
    if index is None:
        raise FlasherError("Could not determine the physical drive number to wipe.")

    log(f"Wiping PhysicalDrive{index} with diskpart (clean) ...")
    script = (
        f"select disk {index}\n"
        "attributes disk clear readonly\n"
        "clean\n"
        "exit\n"
    )
    handle, path = tempfile.mkstemp(suffix=".txt", prefix="banana_diskpart_")
    try:
        with os.fdopen(handle, "w") as fh:
            fh.write(script)
        try:
            proc = subprocess.run(
                ["diskpart", "/s", path],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=300,
            )
        except subprocess.TimeoutExpired as exc:
            raise FlasherError("diskpart timed out while cleaning the disk.") from exc
        output = (proc.stdout or "").strip()
        if proc.returncode != 0 or "DiskPart has encountered an error" in output:
            raise FlasherError(f"diskpart failed:\n{output}")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    log("  Target wiped (diskpart clean completed).")


# ===========================================================================
#  Streaming flasher -- decompress on the fly straight to the block device
# ===========================================================================
def flash_device(
    image_path: str,
    device: dict,
    total_uncompressed: int,
    progress,
    cancel: threading.Event,
    log,
) -> int:
    """
    Decompress `image_path` and stream it to `device`'s raw node.

    Returns the number of bytes written.
    """
    if IS_WINDOWS:
        return _flash_windows(image_path, device, total_uncompressed, progress, cancel, log)
    return _flash_posix(image_path, device, total_uncompressed, progress, cancel, log)


def _stream_with_progress(image_path, total, write_chunk, progress, cancel, start_clock):
    """Shared gzip -> callback loop with cancellation and throttled reporting."""
    done = 0
    last_report = 0.0
    with gzip.open(image_path, "rb") as gz:
        while True:
            if cancel.is_set():
                raise Cancelled()
            chunk = gz.read(CHUNK_SIZE)
            if not chunk:
                break
            write_chunk(chunk)
            done += len(chunk)
            now = time.monotonic()
            if now - last_report >= 0.1:
                last_report = now
                progress(done, total, now - start_clock)
    return done


def _flash_posix(image_path, device, total, progress, cancel, log) -> int:
    """Linux/macOS: pipe the decompressed image into `sudo dd` on a raw device."""
    target = device["raw"] or device["path"]
    log(f"Streaming image to {target} via dd (this can take a while) ...")

    cmd = list(SUDO_PREFIX) + [
        "dd",
        f"of={target}",
        # BSD dd (macOS) wants lowercase 'm'; GNU dd (Linux) wants 'M'.
        "bs=4m" if IS_MACOS else "bs=4M",
        "conv=fsync",         # flush to media before returning
    ]
    if shutil.which("dd") is None:
        raise FlasherError("dd is not available on this system.")

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    start_clock = time.monotonic()
    written = 0

    def write_chunk(chunk: bytes) -> None:
        nonlocal written
        if proc.stdin is None:
            raise FlasherError("Internal error: dd stdin is unavailable.")
        try:
            proc.stdin.write(chunk)
        except (BrokenPipeError, OSError) as exc:
            raise FlasherError(_dd_failure(proc)) from exc
        written += len(chunk)

    try:
        written = _stream_with_progress(
            image_path, total, write_chunk, progress, cancel, start_clock
        )
        if proc.stdin is not None:
            proc.stdin.close()
        proc.wait(timeout=600)
    except Cancelled:
        _kill(proc)
        raise
    except BrokenPipeError as exc:
        raise FlasherError(_dd_failure(proc)) from exc

    if proc.returncode != 0:
        raise FlasherError(_dd_failure(proc))

    with contextlib.suppress(Exception):
        # Extra safety flush on Linux; harmless if `sync` is unavailable.
        subprocess.run(list(SUDO_PREFIX) + ["sync"],
                       stdout=DEVNULL, stderr=DEVNULL, timeout=600)

    if IS_MACOS:
        log("Ejecting the disk ...")
        try:
            _run(["diskutil", "eject", device["path"]],
                 stdout=DEVNULL, stderr=DEVNULL)
        except FlasherError as exc:
            log(f"  eject warning: {exc}")

    return written


def _dd_failure(proc: subprocess.Popen) -> str:
    detail = ""
    try:
        if proc.stderr is not None:
            detail = proc.stderr.read().decode(errors="replace").strip()
    except Exception:
        pass
    code = proc.poll()
    return f"dd failed (exit {code})." + (f"\n{detail}" if detail else "")


def _kill(proc: subprocess.Popen) -> None:
    try:
        proc.kill()
    except Exception:
        pass
    try:
        if proc.stdin is not None:
            proc.stdin.close()
    except Exception:
        pass
    try:
        proc.wait(timeout=10)
    except Exception:
        pass


# -- Windows raw device writing --------------------------------------------
def _flash_windows(image_path, device, total, progress, cancel, log) -> int:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    OPEN_EXISTING = 3
    FSCTL_LOCK_VOLUME = 0x00090018
    FSCTL_DISMOUNT_VOLUME = 0x00090020
    FSCTL_UNLOCK_VOLUME = 0x0009001C
    IOCTL_DISK_UPDATE_PROPERTIES = 0x00070100
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    ]
    kernel32.WriteFile.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    kernel32.SetFilePointerEx.argtypes = [
        wintypes.HANDLE, ctypes.c_longlong,
        ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD,
    ]
    kernel32.DeviceIoControl.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    target = device["path"]
    log(f"Opening raw device {target} for writing ...")

    handle = kernel32.CreateFileW(
        target,
        GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        0,
        None,
    )
    if handle == INVALID_HANDLE_VALUE or handle is None:
        err = ctypes.get_last_error()
        raise FlasherError(
            f"Could not open {target} (Windows error {err}). "
            "Make sure the app is running as Administrator and the card is not "
            "in use by another program."
        )

    try:
        # Take exclusive control so Windows stops caching/holding the volume.
        for control in (FSCTL_LOCK_VOLUME, FSCTL_DISMOUNT_VOLUME):
            returned = wintypes.DWORD(0)
            kernel32.DeviceIoControl(
                handle, control, None, 0, None, 0,
                ctypes.byref(returned), None,
            )

        zero = ctypes.c_longlong(0)
        kernel32.SetFilePointerEx(handle, zero, None, 0)

        start_clock = time.monotonic()

        def write_chunk(chunk: bytes) -> None:
            buffer = ctypes.create_string_buffer(chunk, len(chunk))
            written_count = wintypes.DWORD(0)
            ok = kernel32.WriteFile(
                handle, buffer, len(chunk),
                ctypes.byref(written_count), None,
            )
            if not ok or written_count.value != len(chunk):
                err = ctypes.get_last_error()
                raise FlasherError(
                    f"Write to {target} failed (Windows error {err}). "
                    "The card may have been removed or is failing."
                )

        written = _stream_with_progress(
            image_path, total, write_chunk, progress, cancel, start_clock
        )

        log("Flushing write buffers to the card ...")
        if not kernel32.FlushFileBuffers(handle):
            log("  (FlushFileBuffers reported an issue, continuing)")

        returned = wintypes.DWORD(0)
        kernel32.DeviceIoControl(
            handle, IOCTL_DISK_UPDATE_PROPERTIES, None, 0, None, 0,
            ctypes.byref(returned), None,
        )
    finally:
        returned = wintypes.DWORD(0)
        kernel32.DeviceIoControl(
            handle, FSCTL_UNLOCK_VOLUME, None, 0, None, 0,
            ctypes.byref(returned), None,
        )
        kernel32.CloseHandle(handle)

    return written


# ===========================================================================
#  Background worker -- runs the whole download/wipe/flash pipeline
# ===========================================================================
class FlashWorker(threading.Thread):
    """Executes the flashing pipeline and reports through a queue."""

    def __init__(self, app: "BananaFlasherApp", model: str, device: dict,
                 release: dict, keep_image: bool):
        super().__init__(name="flash-worker", daemon=True)
        self.app = app
        self.model = model
        self.asset_name = MODELS[model]
        self.device = device
        self.release = release
        self.keep_image = keep_image
        self.cancel_event = threading.Event()
        self._emit = app.emit

    # -- reporting helpers -------------------------------------------------
    def log(self, message: str) -> None:
        self._emit(("log", message))

    def status(self, message: str) -> None:
        self._emit(("status", message))

    def progress(self, fraction: float | None, detail: str = "") -> None:
        self._emit(("progress", fraction, detail))

    # -- pipeline ----------------------------------------------------------
    def run(self) -> None:
        tmpdir = None
        image_path = None
        try:
            tmpdir = tempfile.mkdtemp(prefix="banana-flasher_")
            image_path = os.path.join(tmpdir, self.asset_name)

            # ---- 1. Download -------------------------------------------
            self.status("Downloading image ...")
            self.log(
                f"Downloading {self.asset_name} ({self.release.get('tag')}) "
                f"from GitHub ..."
            )
            if self.release.get("size"):
                self.log(f"  Asset size: {format_bytes(self.release['size'])}")
            download_asset(
                self.release["url"], image_path, self._download_progress,
                self.cancel_event,
            )
            self.log(f"  Saved to {image_path}")

            # ---- 1b. Integrity check -----------------------------------
            expected = self.release.get("checksum")
            self.log("Verifying SHA-256 checksum ...")
            self.status("Verifying checksum ...")
            try:
                actual = sha256_file(image_path, self.cancel_event)
            except Cancelled:
                raise
            if expected:
                if actual != expected:
                    raise FlasherError(
                        "Checksum mismatch!\n\n"
                        f"  expected: {expected}\n"
                        f"  actual  : {actual}\n\n"
                        "The download is corrupt or has been tampered with. "
                        "The card was NOT modified."
                    )
                self.log("  Checksum OK.")
            else:
                self.log(f"  No published checksum; computed {actual[:16]}…")

            # ---- 2. Privileges -----------------------------------------
            if not is_elevated() and not SUDO_PREFIX and not IS_WINDOWS:
                raise FlasherError("Elevated privileges are required to flash.")

            # ---- 3. Wipe -----------------------------------------------
            self.status("Wiping target drive ...")
            self.progress(0.0, "Wiping partition table and boot area")
            wipe_device(self.device, self.log)
            if self.cancel_event.is_set():
                raise Cancelled()

            # ---- 4. Flash ----------------------------------------------
            self.status("Flashing image ...")
            total = gzip_uncompressed_size(image_path)
            compressed = os.path.getsize(image_path)
            if total and total < compressed:
                # The gzip ISIZE trailer is stored modulo 2**32, so an image
                # larger than 4 GiB reports a wrapped (too small) value. Fall
                # back to an indeterminate bar rather than lying to the user.
                self.log("  Uncompressed size exceeds 4 GiB; progress is "
                         "shown as an estimate.")
                total = 0
            self.log(
                "Streaming decompressed image directly to "
                f"{self.device['path']} ..."
            )

            start = time.monotonic()
            written = flash_device(
                image_path, self.device, total,
                self._flash_progress, self.cancel_event, self.log,
            )
            elapsed = time.monotonic() - start
            rate = (written / elapsed) if elapsed > 0 else 0
            summary = f"  Wrote {format_bytes(written)} in {format_duration(elapsed)}"
            if rate:
                summary += f"  ({format_bytes(rate)}/s)"
            self.log(summary + ".")

            self.progress(1.0, "Done")
            self.status("Finished.")
            self._emit(("done", {
                "model": self.model,
                "device": self.device,
                "written": written,
                "elapsed": elapsed,
                "tag": self.release.get("tag"),
            }))

        except Cancelled:
            self.log("Operation cancelled by the user.")
            self.status("Cancelled.")
            self._emit(("cancelled", None))
        except FlasherError as exc:
            self.log(f"ERROR: {exc}")
            self.status("Failed.")
            self._emit(("error", str(exc)))
        except Exception as exc:  # pragma: no cover - defensive
            import traceback
            self.log("UNEXPECTED ERROR:\n" + traceback.format_exc())
            self.status("Failed.")
            self._emit(("error", f"Unexpected error: {exc}"))
        finally:
            # Clean up the downloaded image unless the user asked to keep it.
            if tmpdir:
                if self.keep_image and image_path and os.path.exists(image_path):
                    self.log(f"Keeping the downloaded image at: {image_path}")
                else:
                    shutil.rmtree(tmpdir, ignore_errors=True)

    # -- progress adapters -------------------------------------------------
    def _download_progress(self, done: int, total: int) -> None:
        if self.cancel_event.is_set():
            raise Cancelled()
        fraction = (done / total) if total else None
        detail = (
            f"{format_bytes(done)} / {format_bytes(total)}"
            if total else format_bytes(done)
        )
        self.progress(fraction, f"Downloading — {detail}")

    def _flash_progress(self, done: int, total: int, elapsed: float) -> None:
        fraction = (done / total) if total else None
        speed = done / elapsed if elapsed else 0
        detail = f"{format_bytes(done)} written"
        if total:
            detail += f" / {format_bytes(total)}"
        if speed:
            detail += f"  @  {format_bytes(speed)}/s"
            if total and speed:
                detail += f"  —  {format_duration((total - done) / speed)} left"
        self.progress(fraction, f"Flashing — {detail}")


# ===========================================================================
#  GUI
# ===========================================================================
class BananaFlasherApp(tk.Tk):
    POLL_MS = 80

    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.minsize(720, 620)
        self.geometry("760x680")

        self.queue: queue.Queue = queue.Queue()
        self.privileges = Privileges(self.log)
        self.devices: list[dict] = []
        self.release_cache: dict[str, dict] = {}
        self.worker: FlashWorker | None = None
        self._releases_lock = threading.Lock()
        self._closing = False
        self._indeterminate = False

        # Tk variables
        self.model_var = tk.StringVar(value=DEFAULT_MODEL)
        self.device_var = tk.StringVar()
        self.keep_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Ready.")
        self.detail_var = tk.StringVar(value="")
        self.release_var = tk.StringVar(value="Querying latest release ...")
        self.progress_var = tk.DoubleVar(value=0.0)

        self._build_style()
        self._build_ui()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(self.POLL_MS, self._drain_queue)

        # Initial work
        self.refresh_devices()
        self._query_release_async()

    # ------------------------------------------------------------------ style
    def _build_style(self) -> None:
        style = ttk.Style(self)
        for theme in ("clam", "alt", "default"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break

        base = ("Helvetica", 11) if not IS_WINDOWS else ("Segoe UI", 10)
        self._font_base = base
        heading = ("Helvetica", 20, "bold") if not IS_WINDOWS else ("Segoe UI", 18, "bold")
        subheading = ("Helvetica", 10) if not IS_WINDOWS else ("Segoe UI", 9)
        bold = (base[0], base[1], "bold")

        style.configure("Heading.TLabel", font=heading)
        style.configure("Sub.TLabel", font=subheading, foreground="#5b6470")
        style.configure("Section.TLabel", font=bold)
        style.configure("Status.TLabel", font=(base[0], base[1] + 1, "bold"))
        style.configure("Detail.TLabel", foreground="#5b6470")
        style.configure("TButton", padding=(12, 6))
        style.configure("Accent.TButton", font=bold, padding=(14, 8))
        style.configure("TRadiobutton", padding=(4, 3))
        style.configure("TLabelframe", padding=10)
        style.configure("TLabelframe.Label", font=bold)
        style.configure("Horizontal.TProgressbar", thickness=18)

    # --------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=0)
        root.pack(fill="both", expand=True)

        # ---- header
        header = ttk.Frame(root, padding=(20, 16, 20, 8))
        header.pack(fill="x")
        ttk.Label(header, text="🍌 " + APP_NAME, style="Heading.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="Flash BananaLinux to a microSD card for the Anbernic RG DS family.",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        body = ttk.Frame(root, padding=(20, 8, 20, 16))
        body.pack(fill="both", expand=True)

        # ---- step 1 : model
        model_box = ttk.LabelFrame(body, text=" 1 · Choose your handheld ", padding=12)
        model_box.pack(fill="x")
        for name in MODELS:
            ttk.Radiobutton(
                model_box, text=name, value=name,
                variable=self.model_var, command=self._on_model_change,
            ).pack(anchor="w")
        ttk.Label(
            model_box, textvariable=self.release_var, style="Detail.TLabel",
        ).pack(anchor="w", pady=(6, 0))

        # ---- step 2 : target drive
        drive_box = ttk.LabelFrame(body, text=" 2 · Select the target drive ", padding=12)
        drive_box.pack(fill="x", pady=(12, 0))

        drive_row = ttk.Frame(drive_box)
        drive_row.pack(fill="x")
        self.device_combo = ttk.Combobox(
            drive_row, textvariable=self.device_var, state="readonly", font=self._font_base,
        )
        self.device_combo.pack(side="left", fill="x", expand=True)
        self.refresh_btn = ttk.Button(
            drive_row, text="Refresh", command=self.refresh_devices,
        )
        self.refresh_btn.pack(side="left", padx=(8, 0))

        ttk.Label(
            drive_box,
            text="Only physical drives are listed. The system/OS disk is marked and "
                 "cannot be flashed.",
            style="Detail.TLabel", wraplength=640, justify="left",
        ).pack(anchor="w", pady=(6, 0))

        # ---- step 3 : flash
        action_box = ttk.LabelFrame(body, text=" 3 · Wipe and flash ", padding=12)
        action_box.pack(fill="x", pady=(12, 0))

        ttk.Label(action_box, textvariable=self.status_var,
                  style="Status.TLabel").pack(anchor="w")
        self.progress = ttk.Progressbar(
            action_box, variable=self.progress_var, maximum=100.0,
            mode="determinate", style="Horizontal.TProgressbar",
        )
        self.progress.pack(fill="x", pady=(8, 4))
        ttk.Label(action_box, textvariable=self.detail_var,
                  style="Detail.TLabel").pack(anchor="w")

        btn_row = ttk.Frame(action_box)
        btn_row.pack(fill="x", pady=(10, 0))
        self.start_btn = ttk.Button(
            btn_row, text="Download & Flash", style="Accent.TButton",
            command=self.start_flash,
        )
        self.start_btn.pack(side="left")
        self.cancel_btn = ttk.Button(
            btn_row, text="Cancel", command=self.cancel_flash, state="disabled",
        )
        self.cancel_btn.pack(side="left", padx=(8, 0))
        ttk.Checkbutton(
            btn_row, text="Keep downloaded image",
            variable=self.keep_var,
        ).pack(side="right")

        # ---- log
        log_box = ttk.LabelFrame(body, text=" Log ", padding=8)
        log_box.pack(fill="both", expand=True, pady=(12, 0))
        self.log_widget = scrolledtext.ScrolledText(
            log_box, height=9, wrap="word", state="disabled",
            font=("Menlo", 9) if IS_MACOS else ("Consolas", 9) if IS_WINDOWS
            else ("DejaVu Sans Mono", 9),
            background="#111418", foreground="#d7dde3",
            insertbackground="#d7dde3", relief="flat", borderwidth=0,
        )
        self.log_widget.pack(fill="both", expand=True)

        # ---- footer
        footer = ttk.Frame(root, padding=(20, 6, 20, 14))
        footer.pack(fill="x")
        ttk.Label(
            footer,
            text="⚠  Writing to a drive erases everything on it. Double-check the "
                 "selected device before flashing. BananaLinux is a community "
                 "project; this tool is unaffiliated with Anbernic.",
            style="Detail.TLabel", wraplength=700, justify="left",
        ).pack(anchor="w")

    # ------------------------------------------------------------------ utils
    def log(self, message: str) -> None:
        """Thread-safe logging (also used directly during startup)."""
        self.queue.put(("log", message))

    def emit(self, event) -> None:
        self.queue.put(event)

    def _append_log(self, message: str) -> None:
        self.log_widget.configure(state="normal")
        self.log_widget.insert("end", message.rstrip() + "\n")
        self.log_widget.see("end")
        self.log_widget.configure(state="disabled")

    # -------------------------------------------------------------- devices
    def refresh_devices(self) -> None:
        self.status_var.set("Scanning drives ...")
        self.update_idletasks()
        try:
            self.devices = list_devices()
        except Exception as exc:
            self.devices = []
            self.log(f"Failed to enumerate drives: {exc}")

        values = [d["label"] for d in self.devices]
        self.device_combo.configure(values=values)
        if values and self.device_var.get() not in values:
            # Prefer the first non-protected device.
            default = next((d["label"] for d in self.devices if not d["protected"]), values[0])
            self.device_var.set(default)
        elif not values:
            self.device_var.set("")
            self.log("No physical drives detected. Insert your microSD card and press Refresh.")

        self.log(f"Detected {len(self.devices)} physical drive(s).")
        self.status_var.set("Ready.")

    def _selected_device(self) -> dict | None:
        label = self.device_var.get()
        return next((d for d in self.devices if d["label"] == label), None)

    # -------------------------------------------------------------- release
    def _on_model_change(self) -> None:
        self._update_release_label()
        self._query_release_async()

    def _update_release_label(self) -> None:
        asset = MODELS[self.model_var.get()]
        info = self.release_cache.get(asset)
        if info:
            self.release_var.set(
                f"Asset: {asset}   ·   release {info['tag']}   ·   "
                f"{format_bytes(info['size']) if info.get('size') else 'size unknown'}"
            )
        else:
            self.release_var.set(f"Asset: {asset}   ·   checking GitHub ...")

    def _query_release_async(self) -> None:
        model = self.model_var.get()
        asset = MODELS[model]

        with self._releases_lock:
            if asset in self.release_cache:
                self._update_release_label()
                return

        def worker() -> None:
            try:
                info = resolve_release_asset(asset)
                self.release_cache[asset] = info
                self.emit(("release", asset, info, None))
            except FlasherError as exc:
                self.emit(("release", asset, None, str(exc)))
            except Exception as exc:  # pragma: no cover
                self.emit(("release", asset, None, str(exc)))

        threading.Thread(target=worker, name="release-lookup", daemon=True).start()

    # ---------------------------------------------------------------- flash
    def start_flash(self) -> None:
        if self.worker and self.worker.is_alive():
            return

        model = self.model_var.get()
        asset = MODELS[model]
        device = self._selected_device()

        if device is None:
            self._show_error(
                "No drive selected",
                "Please insert a microSD card, press Refresh, and select it from "
                "the dropdown.",
            )
            return

        if device["protected"]:
            self._show_error(
                "Refusing to flash the system disk",
                "The selected drive looks like the disk your operating system is "
                "running from (or it holds mounted volumes). Flashing it would "
                "destroy your computer. Please pick a different device.",
            )
            return

        release = self.release_cache.get(asset)
        if release is None:
            self._show_error(
                "Release not resolved yet",
                f"The latest release information for '{asset}' is not available. "
                "Check your internet connection and try again.",
            )
            return

        if not self._confirm_flash(model, device, release):
            return

        # Elevation up front (Windows relaunch / POSIX sudo password).
        if not self._ensure_privileges():
            return

        self.progress_var.set(0.0)
        self.detail_var.set("")
        self._set_running(True)
        self.log("—" * 60)
        self.log(f"Selected handheld : {model}")
        self.log(f"Selected drive    : {device['label']}")
        self.log(f"Release           : {release['tag']}")

        self.worker = FlashWorker(
            self, model, device, release, keep_image=self.keep_var.get(),
        )
        self.worker.start()

    def _confirm_flash(self, model: str, device: dict, release: dict) -> bool:
        from tkinter import messagebox

        size = format_bytes(device["size"]) if device["size"] else "unknown size"
        checksum_line = ""
        if release.get("checksum"):
            checksum_line = f"\nSHA-256: {release['checksum'][:16]}… (will be verified)\n"
        message = (
            f"You are about to ERASE and overwrite:\n\n"
            f"    {device['path']}\n"
            f"    {device['label']}\n"
            f"    capacity: {size}\n\n"
            f"with:\n\n"
            f"    {MODELS[model]}  (release {release['tag']})\n"
            f"{checksum_line}\n"
            "ALL data on that drive will be permanently destroyed, including its "
            "partition table.\n\nDo you want to continue?"
        )
        return messagebox.askyesno(
            "Confirm destructive write", message, icon="warning", default="no",
        )

    def _ensure_privileges(self) -> bool:
        from tkinter import messagebox

        if IS_WINDOWS:
            if is_elevated():
                return True
            if messagebox.askyesno(
                "Administrator rights required",
                "Flashing a physical drive on Windows requires Administrator "
                "rights.\n\nRelaunch the app elevated now?",
                icon="warning",
            ):
                if Privileges.relaunch_as_admin():
                    self._on_close(silent=True)
                    self.after(50, self.destroy)
                else:
                    self._show_error(
                        "Elevation failed",
                        "Windows refused to elevate the application. Please "
                        "right-click the script/app and choose 'Run as "
                        "administrator'.",
                    )
            return False

        if is_elevated():
            global SUDO_PREFIX
            SUDO_PREFIX = []
            return True

        password = self._ask_sudo_password()
        if password is None:
            return False
        self.status_var.set("Validating privileges ...")
        if self.privileges.cache_sudo(password):
            self.log("Sudo credentials cached; elevated commands are ready.")
            self.status_var.set("Ready.")
            return True

        self.status_var.set("Ready.")
        self._show_error(
            "Authentication failed",
            "sudo rejected the password. Please try again, or run the tool as "
            "root.",
        )
        return False

    def _ask_sudo_password(self) -> str | None:
        """Modal password prompt; returns the password or None if cancelled."""
        dialog = tk.Toplevel(self)
        dialog.title("sudo password required")
        dialog.transient(self)
        dialog.resizable(False, False)
        dialog.grab_set()

        ttk.Label(
            dialog,
            text="Wiping and writing raw block devices needs elevated rights.\n"
                 "Enter your password (it is used only to cache a sudo session):",
            justify="left", padding=(16, 14, 16, 6),
        ).pack(fill="x")

        entry_var = tk.StringVar()
        entry = ttk.Entry(dialog, textvariable=entry_var, show="•", width=36)
        entry.pack(fill="x", padx=16)
        entry.focus_set()

        result: dict = {"value": None}

        def accept(event=None) -> None:
            result["value"] = entry_var.get()
            dialog.destroy()

        def cancel(event=None) -> None:
            result["value"] = None
            dialog.destroy()

        buttons = ttk.Frame(dialog, padding=(16, 12, 16, 14))
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Cancel", command=cancel).pack(side="right")
        ttk.Button(buttons, text="Unlock", style="Accent.TButton",
                   command=accept).pack(side="right", padx=(0, 8))
        dialog.bind("<Return>", accept)
        dialog.bind("<Escape>", cancel)

        self._center(dialog)
        dialog.after(20, entry.focus_force)
        self.wait_window(dialog)
        return result["value"]

    def _center(self, window: tk.Toplevel) -> None:
        window.update_idletasks()
        width = window.winfo_width() or window.winfo_reqwidth()
        height = window.winfo_height() or window.winfo_reqheight()
        x = self.winfo_rootx() + (self.winfo_width() - width) // 2
        y = self.winfo_rooty() + (self.winfo_height() - height) // 3
        window.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def cancel_flash(self) -> None:
        if self.worker and self.worker.is_alive():
            self.worker.cancel_event.set()
            self.status_var.set("Cancelling ...")
            self.log("Cancel requested; stopping after the current block ...")

    # ----------------------------------------------------------- state mgmt
    def _set_running(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        self.start_btn.configure(state=state)
        self.refresh_btn.configure(state=state)
        self.device_combo.configure(state="disabled" if running else "readonly")
        self.cancel_btn.configure(state="normal" if running else "disabled")

    def _reset_progress(self) -> None:
        if self._indeterminate:
            self.progress.stop()
            self._indeterminate = False
        self.progress.configure(mode="determinate")

    def _show_error(self, title: str, message: str) -> None:
        from tkinter import messagebox
        messagebox.showerror(title, message)

    # ------------------------------------------------------------ queue pump
    def _drain_queue(self) -> None:
        # One bad event must never kill the UI pump, hence the inner try/except
        # and the finally-reschedule.
        try:
            while True:
                try:
                    kind, *payload = self.queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle_event(kind, payload)
                except Exception as exc:  # pragma: no cover - defensive
                    self._append_log(f"[ui] error while handling '{kind}': {exc}")
        finally:
            if not self._closing:
                self.after(self.POLL_MS, self._drain_queue)

    def _handle_event(self, kind: str, payload: list) -> None:
        if kind == "log":
            self._append_log(payload[0])
        elif kind == "status":
            self.status_var.set(payload[0])
        elif kind == "progress":
            fraction, detail = payload
            if fraction is None:
                # Indeterminate fallback: animate a sweeping bar (start once).
                if not self._indeterminate:
                    self._indeterminate = True
                    self.progress.configure(mode="indeterminate")
                    self.progress.start(12)
            else:
                if self._indeterminate:
                    self._indeterminate = False
                    self.progress.stop()
                self.progress.configure(mode="determinate")
                self.progress_var.set(max(0.0, min(1.0, fraction)) * 100.0)
            self.detail_var.set(detail)
        elif kind == "release":
            asset, info, error = payload
            if info:
                self.release_cache[asset] = info
                self.log(f"Latest release {info['tag']}: {info['name']} "
                         f"({format_bytes(info['size']) if info['size'] else '?'})")
            else:
                self.log(f"Could not resolve release asset: {error}")
            if MODELS.get(self.model_var.get()) == asset:
                self._update_release_label()
        elif kind == "done":
            self._on_done(payload[0])
        elif kind == "cancelled":
            self._set_running(False)
            self._reset_progress()
            self.detail_var.set("")
            self.status_var.set("Cancelled — the drive may be left blank.")
        elif kind == "error":
            self._set_running(False)
            self._reset_progress()
            self.status_var.set("Failed.")
            self._show_error("Flashing failed", payload[0])

    def _on_done(self, result: dict) -> None:
        from tkinter import messagebox

        self._set_running(False)
        self._reset_progress()
        self.progress_var.set(100.0)
        self.detail_var.set(f"Wrote {format_bytes(result['written'])} "
                            f"in {format_duration(result['elapsed'])}")
        self.status_var.set("Finished — you can remove the card.")

        messagebox.showinfo(
            "Flashing complete",
            f"BananaLinux was written to the microSD card.\n\n"
            f"Handheld : {result['model']}\n"
            f"Drive    : {result['device']['path']}\n"
            f"Release  : {result['tag']}\n"
            f"Written  : {format_bytes(result['written'])}\n"
            f"Time     : {format_duration(result['elapsed'])}\n\n"
            "It is now safe to remove the card and insert it into your device.",
        )

    # ----------------------------------------------------------------- close
    def _on_close(self, silent: bool = False) -> None:
        if self.worker and self.worker.is_alive():
            from tkinter import messagebox
            if not silent and not messagebox.askyesno(
                "Quit while flashing?",
                "A flash operation is still running. Quitting now will leave the "
                "card in an inconsistent state.\n\nQuit anyway?",
                icon="warning",
            ):
                return
            self.worker.cancel_event.set()

        self._closing = True
        self.privileges.shutdown()
        if not silent:
            self.destroy()


# ===========================================================================
#  Entry point
# ===========================================================================
def main() -> int:
    try:
        app = BananaFlasherApp()
    except tk.TclError as exc:
        print(
            f"{APP_NAME}: could not start the GUI ({exc}).\n"
            "A graphical display is required. On a headless server, run the "
            "tools directly or install Tk.",
            file=sys.stderr,
        )
        return 2

    try:
        app.mainloop()
    finally:
        app.privileges.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())