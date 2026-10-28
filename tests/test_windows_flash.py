# -*- coding: utf-8 -*-
"""
Linux-runnable tests for the Windows-specific flashing code.

The Windows write path talks to ``kernel32`` through ``ctypes`` and to
``mountvol`` / PowerShell for the auto-mount guard. None of that exists on
Linux, so these tests replace those boundaries with in-memory fakes and then
exercise the *logic* that is easy to get wrong:

  * the raw streaming write (seeking, short writes, offsets),
  * the retry-on-transient-error behaviour (Windows error 5 etc.),
  * the auto-mount guard's disable + always-restore contract.

They do NOT prove that the real Win32 calls are correct -- only Wine or a real
Windows machine can do that -- but they catch logic regressions on any OS.

Run with either::

    python -m unittest discover -s tests -v
    python -m pytest tests -v
"""

from __future__ import annotations

import ctypes
import gzip
import os
import tempfile
import threading
import types
import unittest
from unittest import mock

import flash_banana as fb


# ---------------------------------------------------------------------------
#  A fake \\\\.\\PhysicalDrive backed by a bytearray
# ---------------------------------------------------------------------------
class FakeDiskState:
    def __init__(self, fail_times=0, fail_code=5, short_write_first=False):
        self.pos = 0                # current raw byte offset
        self.ba = bytearray()       # everything the fake device received
        self.fail_times = fail_times
        self.fail_code = fail_code
        self.short_write_first = short_write_first
        self.error = 0              # what ctypes.get_last_error() should report
        self.seeks = []
        self.write_calls = 0


def make_fake_kernel32(state: FakeDiskState):
    """Return an object shaped like the kernel32 functions _flash_windows uses."""

    def create_file(*args, **kwargs):
        return 1234  # any non-NULL, non-INVALID_HANDLE_VALUE handle

    def write_file(handle, buffer, length, written_ptr, overlapped):
        state.write_calls += 1
        if state.fail_times > 0:
            state.fail_times -= 1
            state.error = state.fail_code
            return 0  # FALSE, error is picked up via get_last_error()

        data = ctypes.string_at(buffer, length)
        count = length
        if state.short_write_first and length > 1:
            count = length // 2
            state.short_write_first = False

        end = state.pos + count
        if end > len(state.ba):
            state.ba.extend(b"\x00" * (end - len(state.ba)))
        state.ba[state.pos:end] = data[:count]
        state.pos = end
        written_ptr._obj.value = count  # byref(...) -> underlying DWORD
        state.error = 0
        return 1

    def set_pointer(handle, position, newpos, method):
        state.pos = position.value
        state.seeks.append(state.pos)
        return 1

    def device_io(handle, control, in_buf, in_len, out_buf, out_len,
                  returned, overlapped):
        return 1

    def always_ok(handle):
        return 1

    def find_first_volume(buffer, size):
        return ctypes.c_void_p(-1).value  # INVALID_HANDLE_VALUE: no volumes

    def find_next_volume(find, buffer, size):
        return 0

    kernel = types.SimpleNamespace(
        CreateFileW=create_file,
        WriteFile=write_file,
        SetFilePointerEx=set_pointer,
        DeviceIoControl=device_io,
        FlushFileBuffers=always_ok,
        CloseHandle=always_ok,
        FindFirstVolumeW=find_first_volume,
        FindNextVolumeW=find_next_volume,
        FindVolumeClose=always_ok,
    )
    # _flash_windows assigns .restype/.argtypes on these; harmless on fakes.
    for fn in vars(kernel).values():
        fn.restype = None
        fn.argtypes = []
    return kernel


def _make_image(folder: str, content: bytes) -> str:
    path = os.path.join(folder, "banana.img.gz")
    with gzip.open(path, "wb") as handle:
        handle.write(content)
    return path


class FlashWindowsWriteTests(unittest.TestCase):
    """Exercise _flash_windows against a fake device handle."""

    def setUp(self):
        # 2.5 MiB of patterned data -> several 1 MiB streaming chunks.
        self.content = bytes(range(256)) * (10 * 1024)
        self._tmp = tempfile.TemporaryDirectory(prefix="banana-test_")
        self.image = _make_image(self._tmp.name, self.content)
        self.logs: list[str] = []

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, state: FakeDiskState) -> int:
        kernel = make_fake_kernel32(state)
        with mock.patch.object(ctypes, "WinDLL", create=True,
                               new=lambda *a, **k: kernel), \
             mock.patch.object(ctypes, "get_last_error", create=True,
                               new=lambda: state.error), \
             mock.patch.object(fb.time, "sleep", new=lambda *_: None):
            return fb._flash_windows(
                self.image,
                {"path": r"\\.\PhysicalDrive2", "index": 2},
                len(self.content),
                lambda *args: None,
                threading.Event(),
                self.logs.append,
            )

    def test_clean_write_produces_exact_image(self):
        state = FakeDiskState()
        written = self._run(state)
        self.assertEqual(written, len(self.content))
        self.assertEqual(bytes(state.ba), self.content)
        self.assertEqual(state.seeks[0], 0)

    def test_short_write_is_completed_at_correct_offset(self):
        state = FakeDiskState(short_write_first=True)
        self._run(state)
        self.assertEqual(bytes(state.ba), self.content)
        # A short write must force at least one extra WriteFile call.
        self.assertGreater(state.write_calls, len(self.content) // (1 << 20))

    def test_transient_access_denied_is_retried(self):
        # Three consecutive error-5 failures, then the write succeeds.
        state = FakeDiskState(fail_times=3, fail_code=5)
        written = self._run(state)
        self.assertEqual(written, len(self.content))
        self.assertEqual(bytes(state.ba), self.content)
        self.assertTrue(any("Retrying write" in m for m in self.logs))

    def test_non_retryable_error_fails_with_offset(self):
        state = FakeDiskState(fail_times=1, fail_code=23)  # ERROR_CRC
        with self.assertRaises(fb.FlasherError) as ctx:
            self._run(state)
        message = str(ctx.exception)
        self.assertIn("Windows error 23", message)
        self.assertIn("offset", message)

    def test_access_denied_gives_up_after_retry_budget(self):
        # More failures than the retry budget, all on the very first block.
        state = FakeDiskState(fail_times=9, fail_code=5)
        with self.assertRaises(fb.FlasherError) as ctx:
            self._run(state)
        self.assertIn("Windows error 5", str(ctx.exception))


# ---------------------------------------------------------------------------
#  The auto-mount guard
# ---------------------------------------------------------------------------
class RawAccessGuardTests(unittest.TestCase):
    def _patches(self, *, automount=True, service=True, events=None):
        events = events if events is not None else []

        def set_automount(enabled):
            events.append(("automount", enabled))
            return True

        def set_service(running):
            events.append(("service", running))
            return True

        return [
            mock.patch.object(fb, "IS_WINDOWS", True),
            mock.patch.object(fb, "_automount_enabled", return_value=automount),
            mock.patch.object(fb, "_set_automount", side_effect=set_automount),
            mock.patch.object(fb, "_shell_hw_detection_running",
                              return_value=service),
            mock.patch.object(fb, "_set_shell_hw_detection",
                              side_effect=set_service),
        ], events

    def _enter(self, patches):
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_disables_then_restores(self):
        patches, events = self._patches()
        self._enter(patches)
        with fb.WindowsRawAccessGuard(lambda _m: None):
            pass
        self.assertEqual(events, [
            ("automount", False),
            ("service", False),
            ("service", True),
            ("automount", True),
        ])

    def test_restores_even_when_body_raises(self):
        patches, events = self._patches()
        self._enter(patches)
        with self.assertRaises(RuntimeError):
            with fb.WindowsRawAccessGuard(lambda _m: None):
                raise RuntimeError("boom")
        self.assertIn(("automount", True), events)
        self.assertIn(("service", True), events)

    def test_leaves_already_disabled_settings_alone(self):
        patches, events = self._patches(automount=False, service=False)
        self._enter(patches)
        with fb.WindowsRawAccessGuard(lambda _m: None):
            pass
        # Nothing was disabled by us, so nothing should be re-enabled.
        self.assertEqual(events, [])

    def test_noop_on_non_windows(self):
        events = []
        with mock.patch.object(fb, "IS_WINDOWS", False), \
             mock.patch.object(fb, "_set_automount",
                               side_effect=lambda e: events.append(e)), \
             mock.patch.object(fb, "_set_shell_hw_detection",
                               side_effect=lambda r: events.append(r)):
            with fb.WindowsRawAccessGuard(lambda _m: None):
                pass
        self.assertEqual(events, [])


# ---------------------------------------------------------------------------
#  Volume enumeration / locking
# ---------------------------------------------------------------------------
IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS = 0x00560000
FSCTL_LOCK_VOLUME = 0x00090018
FSCTL_DISMOUNT_VOLUME = 0x00090020
FSCTL_UNLOCK_VOLUME = 0x0009001C


class FakeVolumeKernel32:
    """Minimal kernel32 fake exposing a set of volumes on given disks."""

    def __init__(self, volumes, lock_ok=True):
        self.volumes = list(volumes)          # [(name, disk_number), ...]
        self.lock_ok = lock_ok
        self._cursor = -1
        self._next = 100
        self.by_handle = {}                   # handle -> (name, disk)
        self.locked = []
        self.dismounted = []
        self.unlocked = []
        self.closed = []

        def find_first_volume(buffer, size):
            if not self.volumes:
                return ctypes.c_void_p(-1).value
            self._cursor = 0
            buffer.value = self.volumes[0][0]
            return 0xF1

        def find_next_volume(find, buffer, size):
            self._cursor += 1
            if self._cursor < len(self.volumes):
                buffer.value = self.volumes[self._cursor][0]
                return 1
            return 0

        def create_file(name, access, share, sec, disp, flags, tmpl):
            self._next += 1
            handle = self._next
            disk = next((d for n, d in self.volumes if n == name), None)
            self.by_handle[handle] = (name, disk)
            return handle

        def device_io(handle, code, inbuf, inlen, outbuf, outlen, returned,
                      overlapped):
            if code == IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS:
                extents = outbuf._obj
                extents.NumberOfDiskExtents = 1
                extents.Extents[0].DiskNumber = self.by_handle[handle][1]
                return 1
            if code == FSCTL_LOCK_VOLUME:
                if not self.lock_ok:
                    return 0
                self.locked.append(handle)
                return 1
            if code == FSCTL_DISMOUNT_VOLUME:
                if not self.lock_ok:
                    return 0
                self.dismounted.append(handle)
                return 1
            if code == FSCTL_UNLOCK_VOLUME:
                self.unlocked.append(handle)
                return 1
            return 1

        def close(handle):
            self.closed.append(handle)
            return 1

        self.CreateFileW = create_file
        self.DeviceIoControl = device_io
        self.FindFirstVolumeW = find_first_volume
        self.FindNextVolumeW = find_next_volume
        self.FindVolumeClose = close
        self.CloseHandle = close
        for fn in (create_file, device_io, find_first_volume, find_next_volume,
                   close):
            fn.restype = None
            fn.argtypes = []


class VolumeLockTests(unittest.TestCase):
    def setUp(self):
        # ctypes.get_last_error() is Windows-only; stub it for the failure logs.
        patcher = mock.patch.object(ctypes, "get_last_error", create=True,
                                    new=lambda: 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_locks_and_dismounts_only_target_disk_volumes(self):
        volumes = [
            ("\\\\?\\Volume{aaa}\\", 5),
            ("\\\\?\\Volume{bbb}\\", 5),
            ("\\\\?\\Volume{ccc}\\", 0),
        ]
        k = FakeVolumeKernel32(volumes)
        held = fb._lock_and_dismount_volumes(k, 5, lambda _m: None)

        self.assertEqual(len(held), 2)
        self.assertEqual(sorted(k.locked), sorted(held))
        self.assertEqual(sorted(k.dismounted), sorted(held))

        other = next(h for h, (n, d) in k.by_handle.items() if d == 0)
        self.assertIn(other, k.closed)      # opened, then closed untouched
        self.assertNotIn(other, k.locked)

    def test_release_unlocks_and_closes(self):
        k = FakeVolumeKernel32([("\\\\?\\Volume{aaa}\\", 5)])
        held = fb._lock_and_dismount_volumes(k, 5, lambda _m: None)
        fb._unlock_and_close_volumes(k, held)
        self.assertEqual(k.unlocked, held)
        for handle in held:
            self.assertIn(handle, k.closed)

    def test_lock_failure_is_skipped(self):
        k = FakeVolumeKernel32([("\\\\?\\Volume{aaa}\\", 5)], lock_ok=False)
        held = fb._lock_and_dismount_volumes(k, 5, lambda _m: None)
        self.assertEqual(held, [])
        self.assertEqual(k.locked, [])
        self.assertTrue(k.closed)           # un-lockable handle was released

    def test_no_volumes_is_handled(self):
        k = FakeVolumeKernel32([])
        held = fb._lock_and_dismount_volumes(k, 5, lambda _m: None)
        self.assertEqual(held, [])


if __name__ == "__main__":
    unittest.main()
