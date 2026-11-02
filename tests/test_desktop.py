from pathlib import Path
import importlib.util
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('desktop',ROOT/'desktop/app.py')
app=importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

class DesktopGates(unittest.TestCase):
    def test_unconfirmed_write_does_not_start_node(self):
        with patch.object(app,'node_run') as node:
            for operation in ['flash','restore']:
                for force in [False,True]:
                    with self.assertRaises(ValueError):app.maintain(operation,'missing','missing',force=force)
            node.assert_not_called()

    def test_wrong_firmware_rejected_before_device_access(self):
        with tempfile.TemporaryDirectory() as t, patch.object(app,'node_run') as node:
            p=Path(t)/'wrong.bin';p.write_bytes(bytes(491520))
            for operation in ['plan','verify','flash','restore']:
                with self.assertRaises(ValueError):app.maintain(operation,p,p,yes=True)
            node.assert_not_called()

    def test_source_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as t:
            source=Path(t)/'source.bin';source.write_bytes(b'original')
            with self.assertRaises(ValueError):app.generate(source,source,overwrite=True)
            self.assertEqual(source.read_bytes(),b'original')

    def test_invalid_backup_leaves_existing_output_intact(self):
        with tempfile.TemporaryDirectory() as t:
            source,out=Path(t)/'source.bin',Path(t)/'existing.bin'
            source.write_bytes(bytes(491520));out.write_bytes(b'keep this')
            with self.assertRaises(ValueError):app.generate(source,out,overwrite=True)
            self.assertEqual(out.read_bytes(),b'keep this')

    def test_force_generation_and_maintenance_propagate_without_weakening_target_check(self):
        with tempfile.TemporaryDirectory() as t, patch.object(app,'node_run',return_value='PASS') as node:
            source,out=Path(t)/'unknown.bin',Path(t)/'forced.bin'
            source.write_bytes(bytes(491520))
            result=app.generate(source,out,force=True)
            self.assertTrue(result['force'])
            self.assertEqual(source.read_bytes(),bytes(491520))
            app.maintain('flash',source,out,yes=True,force=True)
            self.assertIn('--force',node.call_args.args[1])
            self.assertIn('--yes',node.call_args.args[1])
            node.reset_mock()
            with self.assertRaises(ValueError):app.maintain('flash',source,out,yes=True)
            bad=bytearray(out.read_bytes());bad[20000]^=1;out.write_bytes(bad)
            with self.assertRaises(ValueError):app.maintain('flash',source,out,yes=True,force=True)
            node.assert_not_called()

    def test_force_cannot_overwrite_source(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'source.bin';p.write_bytes(bytes(491520))
            with self.assertRaises(ValueError):app.generate(p,p,overwrite=True,force=True)
            self.assertEqual(p.read_bytes(),bytes(491520))

if __name__=='__main__':unittest.main()
