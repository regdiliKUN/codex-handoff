import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location('build_exe', ROOT / 'build_exe.py')
build_exe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_exe)
spec = importlib.util.spec_from_file_location('checksums', ROOT / 'checksums.py')
checksums = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checksums)


class BuildScriptTests(unittest.TestCase):
    def test_version_tuple_is_four_parts(self):
        self.assertEqual(build_exe.version_tuple('1.2.0'), (1, 2, 0, 0))
        self.assertEqual(build_exe.version_tuple('2.7'), (2, 7, 0, 0))
        self.assertEqual(build_exe.version_tuple('1.2.3.4.5'), (1, 2, 3, 4))

    def test_version_resource_contains_release(self):
        text = build_exe.version_info_text('1.2.0')
        self.assertIn('filevers=(1, 2, 0, 0)', text)
        self.assertIn("StringStruct('FileVersion', '1.2.0.0')", text)
        self.assertIn('Codex 交接助手', text)

    def test_build_command_includes_icon_resources_and_version(self):
        command = build_exe.build_command('python', version_file='version.txt')
        self.assertIn('--onefile', command)
        self.assertIn('--windowed', command)
        self.assertIn(str(build_exe.ICON), command)
        self.assertIn('--version-file', command)
        self.assertIn('version.txt', command)
        self.assertTrue(any('resources' in part and 'handoff.ico' not in part for part in command))
        self.assertEqual(command[-1], str(build_exe.ROOT / 'handoff.py'))

    def test_checksums_round_trip_and_detect_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for relative in checksums.FILES:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('content', encoding='utf-8')
            output = checksums.write(root)
            self.assertEqual(checksums.verify(root, output), [])
            (root / 'handoff.py').write_text('changed', encoding='utf-8')
            problems = checksums.verify(root, output)
            self.assertTrue(any('handoff.py' in item for item in problems))


if __name__ == '__main__':
    unittest.main(verbosity=2)
