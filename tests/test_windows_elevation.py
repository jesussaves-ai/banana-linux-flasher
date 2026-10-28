# -*- coding: utf-8 -*-
"""
Linux-runnable tests for the on-demand Windows elevation path.

The privileged helper (``_privileged_write_main``) and the IPC helpers are
plain Python, so they run anywhere. The UAC spawn (``_spawn_elevated_write``)
is driven through a fake ``shell32``/``kernel32`` so the parent-side wait loop,
status relay and result handling are all exercised without Windows.

Run with either::

    python -m unittest discover -s tests -v
    python -m pytest tests -v
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

import flash_banana as fb


DEVICE = {
    "path": r"\\.\PhysicalDrive2",
    "raw": r"\\.\PhysicalDrive2",
    "label": r"\\.\PhysicalDrive2  (SD)",
    "size": 32 * 1024 ** 3,
    "removable": True,
    "protected": False,
    "detail": "SD USB",
    "index": 2,
}


class WorkerRequestMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="banana-elev_")
        self.addCleanup(self._tmp.cleanup)

    def _make_request(self, device=None, image=True, total=4096):
        workdir = self._tmp.name
        image_path = os.path.join(workdir, "banana.img.gz")
        if image:
            with open(image_path, "wb") as handle:
                handle.write(b"\x1f\x8b fake image")
        request = {
            "image": image_path if image else os.path.join(workdir, "missing"),
            "device": dict(device or DEVICE),
            "total": total,
            "status_file": os.path.join(workdir, "status.ndjson"),
            "result_file": os.path.join(workdir, "result.json"),
            "log_file": os.path.join(workdir, "worker.log"),
        }
        request_path = os.path.join(workdir, "request.json")
        with open(request_path, "w", encoding="utf-8") as handle:
            json.dump(request, handle)
        return request_path, request


# ---------------------------------------------------------------------------
#  Privileged helper process
# ---------------------------------------------------------------------------
class PrivilegedWorkerTests(WorkerRequestMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        # The helper prints its log to stdout; keep the test output tidy.
        patcher = mock.patch.object(sys, "stdout", None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, request_path):
        with mock.patch.object(fb, "is_elevated", return_value=True), \
             mock.patch.object(fb, "wipe_device") as wipe, \
             mock.patch.object(fb, "flash_device", side_effect=self._fake_flash):
            self.wipe = wipe
            return fb._privileged_write_main(request_path)

    @staticmethod
    def _fake_flash(image_path, device, total, progress, cancel, log):
        progress(2048, 4096, 0.5)
        progress(4096, 4096, 1.0)
        return 4096

    def test_success_writes_result_and_status(self):
        request_path, request = self._make_request()
        rc = self._run(request_path)

        self.assertEqual(rc, 0)
        self.wipe.assert_called_once()
        with open(request["result_file"], encoding="utf-8") as handle:
            result = json.load(handle)
        self.assertTrue(result["ok"])
        self.assertEqual(result["written"], 4096)

        with open(request["status_file"], encoding="utf-8") as handle:
            kinds = [json.loads(line)["kind"] for line in handle if line.strip()]
        self.assertIn("progress", kinds)
        self.assertIn("done", kinds)

    def test_flash_error_is_reported(self):
        request_path, request = self._make_request()

        def boom(*args, **kwargs):
            raise fb.FlasherError("simulated write failure")

        with mock.patch.object(fb, "is_elevated", return_value=True), \
             mock.patch.object(fb, "wipe_device"), \
             mock.patch.object(fb, "flash_device", side_effect=boom):
            rc = fb._privileged_write_main(request_path)

        self.assertEqual(rc, 1)
        with open(request["result_file"], encoding="utf-8") as handle:
            result = json.load(handle)
        self.assertFalse(result["ok"])
        self.assertIn("simulated write failure", result["error"])

    def test_refuses_protected_device(self):
        request_path, request = self._make_request(
            device={**DEVICE, "protected": True})
        with mock.patch.object(fb, "is_elevated", return_value=True):
            rc = fb._privileged_write_main(request_path)
        self.assertEqual(rc, 1)
        with open(request["result_file"], encoding="utf-8") as handle:
            self.assertFalse(json.load(handle)["ok"])

    def test_refuses_unexpected_target(self):
        request_path, request = self._make_request(
            device={**DEVICE, "path": r"C:\Windows"})
        with mock.patch.object(fb, "is_elevated", return_value=True):
            rc = fb._privileged_write_main(request_path)
        self.assertEqual(rc, 1)

    def test_refuses_missing_image(self):
        request_path, request = self._make_request(image=False)
        with mock.patch.object(fb, "is_elevated", return_value=True):
            rc = fb._privileged_write_main(request_path)
        self.assertEqual(rc, 1)
        with open(request["result_file"], encoding="utf-8") as handle:
            self.assertIn("missing", json.load(handle)["error"].lower())

    def test_refuses_when_not_elevated(self):
        request_path, request = self._make_request()
        with mock.patch.object(fb, "is_elevated", return_value=False):
            rc = fb._privileged_write_main(request_path)
        self.assertEqual(rc, 1)
        with open(request["result_file"], encoding="utf-8") as handle:
            self.assertIn("not elevated", json.load(handle)["error"])


# ---------------------------------------------------------------------------
#  IPC helpers
# ---------------------------------------------------------------------------
class IpcHelperTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="banana-ipc_")
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "status.ndjson")

    def test_read_status_ignores_partial_trailing_line(self):
        with open(self.path, "wb") as handle:
            handle.write(b'{"kind": "log", "message": "one"}\n')
            handle.write(b'{"kind": "log", "message": "tw')  # incomplete

        seen = []
        offset = fb._read_status_updates(self.path, 0, seen.append)
        self.assertEqual([e["message"] for e in seen], ["one"])

        # Completing the line lets the next poll pick it up.
        with open(self.path, "ab") as handle:
            handle.write(b'o"}\n')
        fb._read_status_updates(self.path, offset, seen.append)
        self.assertEqual([e["message"] for e in seen], ["one", "two"])

    def test_read_status_missing_file_is_noop(self):
        self.assertEqual(
            fb._read_status_updates(os.path.join(self._tmp.name, "nope"), 0,
                                    lambda _e: None),
            0)

    def test_read_worker_result_variants(self):
        self.assertFalse(fb._read_worker_result(None)["ok"])
        self.assertFalse(
            fb._read_worker_result(os.path.join(self._tmp.name, "nope"))["ok"])
        good = os.path.join(self._tmp.name, "ok.json")
        with open(good, "w", encoding="utf-8") as handle:
            json.dump({"ok": True, "written": 7}, handle)
        self.assertEqual(fb._read_worker_result(good)["written"], 7)

    def test_dispatch_routes_events(self):
        progress, status, log = [], [], []
        fb._dispatch_worker_event(
            {"kind": "progress", "done": 5, "total": 10, "elapsed": 1.5},
            lambda *a: progress.append(a), status.append, log.append)
        fb._dispatch_worker_event({"kind": "status", "message": "hi"},
                                  lambda *a: None, status.append, log.append)
        fb._dispatch_worker_event({"kind": "log", "message": "yo"},
                                  lambda *a: None, status.append, log.append)
        self.assertEqual(progress, [(5, 10, 1.5)])
        self.assertEqual(status, ["hi"])
        self.assertEqual(log, ["yo"])


# ---------------------------------------------------------------------------
#  Parent-side UAC spawn
# ---------------------------------------------------------------------------
class FakeShell32:
    def __init__(self, fail=False):
        self.fail = fail

        def shell_execute_ex(info_ptr):
            if self.fail:
                return 0
            info_ptr._obj.hProcess = 4242
            return 1

        self.ShellExecuteExW = shell_execute_ex
        shell_execute_ex.argtypes = []
        shell_execute_ex.restype = None


class FakeKernel32:
    def __init__(self):
        self.terminated = []

        def wait(_handle, _ms):
            return 0  # WAIT_OBJECT_0

        def get_exit(_handle, code_ptr):
            code_ptr._obj.value = 0
            return 1

        def terminate(handle, _code):
            self.terminated.append(handle)
            return 1

        def close(_handle):
            return 1

        for name, fn in (("WaitForSingleObject", wait),
                         ("GetExitCodeProcess", get_exit),
                         ("TerminateProcess", terminate),
                         ("CloseHandle", close)):
            setattr(self, name, fn)
            fn.argtypes = []
            fn.restype = None


class SpawnElevatedWriteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="banana-spawn_")
        self.addCleanup(self._tmp.cleanup)
        self.status = os.path.join(self._tmp.name, "status.ndjson")
        self.result = os.path.join(self._tmp.name, "result.json")
        self.request = os.path.join(self._tmp.name, "request.json")
        with open(self.request, "w", encoding="utf-8") as handle:
            json.dump({"device": DEVICE}, handle)

    def _run(self, shell, kernel, cancel=None, last_error=0):
        progress, status, log = [], [], []
        patchers = [
            mock.patch.object(ctypes, "WinDLL", create=True,
                              new=lambda name, **kw: shell
                              if name == "shell32" else kernel),
            mock.patch.object(ctypes, "get_last_error", create=True,
                              new=lambda: last_error),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        written = fb._spawn_elevated_write(
            self.request, self.status, self.result,
            lambda *a: progress.append(a), status.append, log.append,
            cancel or threading.Event())
        return written, progress, status, log

    def test_success_relays_progress_and_returns_written(self):
        with open(self.status, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(
                {"kind": "progress", "done": 10, "total": 20,
                 "elapsed": 0.5}) + "\n")
        with open(self.result, "w", encoding="utf-8") as handle:
            json.dump({"ok": True, "written": 9999}, handle)

        written, progress, _status, _log = self._run(
            FakeShell32(), FakeKernel32())
        self.assertEqual(written, 9999)
        self.assertEqual(progress, [(10, 20, 0.5)])

    def test_declined_uac_raises_friendly_error(self):
        with self.assertRaises(fb.FlasherError) as ctx:
            self._run(FakeShell32(fail=True), FakeKernel32(),
                      last_error=fb.ERROR_CANCELLED)
        self.assertIn("dismissed", str(ctx.exception))

    def test_failed_helper_result_raises(self):
        with open(self.result, "w", encoding="utf-8") as handle:
            json.dump({"ok": False, "error": "disk on fire"}, handle)
        with self.assertRaises(fb.FlasherError) as ctx:
            self._run(FakeShell32(), FakeKernel32())
        self.assertIn("disk on fire", str(ctx.exception))

    def test_missing_result_is_an_error(self):
        with self.assertRaises(fb.FlasherError):
            self._run(FakeShell32(), FakeKernel32())

    def test_cancel_terminates_helper(self):
        kernel = FakeKernel32()
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(fb.Cancelled):
            self._run(FakeShell32(), kernel, cancel=cancel)
        self.assertEqual(kernel.terminated, [4242])


# ---------------------------------------------------------------------------
#  main() dispatch
# ---------------------------------------------------------------------------
class MainDispatchTests(unittest.TestCase):
    def test_privileged_flag_routes_to_worker(self):
        with mock.patch.object(sys, "argv",
                               ["flash_banana.py", "--privileged-write",
                                "req.json"]), \
             mock.patch.object(fb, "_privileged_write_main",
                               return_value=0) as worker:
            self.assertEqual(fb.main(), 0)
            worker.assert_called_once_with("req.json")


if __name__ == "__main__":
    unittest.main()
