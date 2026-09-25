import argparse
import hashlib
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("setup", str(ROOT / "bin/herdr-command-setup"))
spec = importlib.util.spec_from_loader(loader.name, loader)
setup = importlib.util.module_from_spec(spec)
loader.exec_module(setup)


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.args = argparse.Namespace(check=False, binary=None, no_path=True)
        self.platform = patch.object(setup.platform, "system", return_value="Linux")
        self.arch = patch.object(setup.platform, "machine", return_value="x86_64")
        self.find = patch.object(setup.shutil, "which", return_value=None)
        for mock in (self.platform, self.arch, self.find):
            mock.start()
            self.addCleanup(mock.stop)

    def test_existing_compatible_herdr_needs_no_network_or_replacement(self):
        with patch.object(setup, "existing", return_value="/bin/herdr"), patch.object(setup, "version", return_value=(0, 9, 2)), patch.object(setup, "run", return_value="herdr 0.9.2") as run:
            setup.install_vm(self.home, self.args)
        self.assertEqual(run.call_args[0][0], ["/bin/herdr", "--version"])
        self.assertFalse((self.home / ".local").exists())

    def test_check_missing_is_read_only(self):
        self.args.check = True
        with self.assertRaisesRegex(RuntimeError, "missing"):
            setup.install_vm(self.home, self.args)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_bad_checksum_preserves_existing_binary(self):
        target = self.home / ".local/bin/herdr"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"old")
        download = self.home / "download"
        download.write_bytes(b"bad")
        self.args.binary = str(download)
        with patch.object(setup, "version", return_value=(0, 8, 0)), self.assertRaisesRegex(RuntimeError, "SHA256 mismatch"):
            setup.install_vm(self.home, self.args)
        self.assertEqual(target.read_bytes(), b"old")
        self.assertEqual(list(target.parent.iterdir()), [target])

    def test_offline_install_verified_before_replacement_and_backs_up(self):
        target = self.home / ".local/bin/herdr"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"old")
        download = self.home / "download"
        download.write_bytes(b"new")
        self.args.binary = str(download)
        with patch.dict(setup.SHA256, {"x86_64": hashlib.sha256(b"new").hexdigest()}), patch.object(setup, "version", side_effect=[(0, 8, 0), (0, 9, 1)]):
            setup.install_vm(self.home, self.args)
        self.assertEqual(target.read_bytes(), b"new")
        self.assertTrue(os.access(str(target), os.X_OK))
        self.assertEqual(next(target.parent.glob("herdr.before-*")).read_bytes(), b"old")

    def test_download_failure_preserves_binary_and_explains_offline_fallback(self):
        with patch.object(setup.shutil, "which", return_value="/bin/curl"), patch.object(setup, "existing", return_value=None), patch.object(setup, "run", side_effect=RuntimeError("SSL timeout")), self.assertRaisesRegex(RuntimeError, "--binary FILE"):
            setup.install_vm(self.home, self.args)
        self.assertFalse((self.home / ".local/bin/herdr").exists())

    def test_wrong_version_after_checksum_is_not_installed(self):
        download = self.home / "download"
        download.write_bytes(b"new")
        self.args.binary = str(download)
        with patch.dict(setup.SHA256, {"x86_64": hashlib.sha256(b"new").hexdigest()}), patch.object(setup, "version", return_value=(0, 8, 0)), self.assertRaisesRegex(RuntimeError, "unexpected version"):
            setup.install_vm(self.home, self.args)
        self.assertFalse((self.home / ".local/bin/herdr").exists())

    def test_tcsh_preserves_cshrc_fallback_and_is_idempotent(self):
        rc = self.home / ".cshrc"
        rc.write_text("# Existing setup\nset prompt='test'\n")
        setup.path_setup(self.home, "/bin/tcsh")
        first = rc.read_text()
        setup.path_setup(self.home, "/bin/tcsh")
        self.assertEqual(rc.read_text(), first)
        self.assertIn('setenv PATH', first)
        self.assertIn("set prompt='test'", first)
        self.assertFalse((self.home / ".tcshrc").exists())
        self.assertEqual(len(list(self.home.glob(".cshrc.before-*"))), 1)

    def test_bash_path_before_noninteractive_guard_preserves_symlink(self):
        actual = self.home / "dotfile"
        actual.write_text("# early return\nreturn\n")
        (self.home / ".bashrc").symlink_to(actual)
        (self.home / ".bash_profile").write_text("# login\n")
        setup.path_setup(self.home, "/bin/bash")
        self.assertTrue((self.home / ".bashrc").is_symlink())
        self.assertTrue(actual.read_text().startswith(setup.MARKER))
        self.assertIn(setup.MARKER, (self.home / ".bash_profile").read_text())

    def test_local_link_and_repeat_keep_same_checkout(self):
        with patch.object(setup.shutil, "which", return_value="/bin/herdr"), patch.object(setup, "version", return_value=(0, 9, 1)), patch.object(setup, "run", return_value="linked"):
            setup.install_local(self.home, self.args)
            setup.install_local(self.home, self.args)
        self.assertEqual((self.home / ".local/bin/herdr-command").resolve(), ROOT / "bin/herdr-command")

    def test_local_conflict_leaves_file_and_does_not_link_plugin(self):
        target = self.home / ".local/bin/herdr-command"
        target.parent.mkdir(parents=True)
        target.write_text("custom")
        with patch.object(setup.shutil, "which", return_value="/bin/herdr"), patch.object(setup, "version", return_value=(0, 9, 1)), patch.object(setup, "run") as run, self.assertRaisesRegex(RuntimeError, "points elsewhere"):
            setup.install_local(self.home, self.args)
        run.assert_not_called()
        self.assertEqual(target.read_text(), "custom")

    def test_unsupported_arch_rejected_without_writes(self):
        with patch.object(setup.platform, "machine", return_value="unsupported"), self.assertRaisesRegex(RuntimeError, "architecture"):
            setup.install_vm(self.home, self.args)
        self.assertEqual(list(self.home.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
