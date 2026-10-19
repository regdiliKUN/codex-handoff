import contextlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).parents[1] / 'handoff.py'
spec = importlib.util.spec_from_file_location('handoff', SOURCE)
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


@contextlib.contextmanager
def database(path):
    c = sqlite3.connect(path)
    try:
        with c:
            yield c
    finally:
        c.close()


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='handoff-test-', dir=Path(__file__).parent)
        self.root = Path(self.temp.name)
        self.store = self.root / 'transfer'
        self.a = self.engine('A')
        self.b = self.engine('B')
        self.init(self.a, 'hello A')

    def tearDown(self):
        self.temp.cleanup()

    def engine(self, name):
        profile = self.root / name
        return h.Engine(profile / '.codex', self.root / (name + '-tool'), str(profile), guard=lambda: None, log=lambda x: None)

    def init(self, engine, text):
        engine.home.mkdir(parents=True)
        sessions = engine.home / 'sessions' / '2026'
        sessions.mkdir(parents=True)
        (sessions / 'one.jsonl').write_text(json.dumps({'type': 'session_meta', 'payload': {'cwd': str(Path(engine.profile) / 'project')}}) + '\n' + text, encoding='utf-8')
        (engine.home / 'auth.json').write_text('LOCAL TOKEN', encoding='utf-8')
        (engine.home / 'config.toml').write_text('LOCAL CONFIG', encoding='utf-8')
        h.atomic_json(engine.home / '.codex-global-state.json', {'local-projects': [{'path': str(Path(engine.profile) / 'project')}], 'machine-only': 'retained', 'queued-follow-ups': ['do not migrate']})
        with database(engine.home / 'state_5.sqlite') as c:
            c.executescript('''CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT, rollout_path TEXT, title TEXT, updated_at INTEGER);
                              CREATE TABLE project_roots (project_id TEXT, path TEXT);
                              CREATE TABLE remote_control_enrollments (token TEXT);
                              CREATE TABLE thread_attachments (identity_key TEXT, payload TEXT);''')
            c.execute('INSERT INTO threads VALUES (?,?,?,?,?)', ('one', str(Path(engine.profile) / 'project'), str(sessions / 'one.jsonl'), text, 1))
            c.execute('INSERT INTO project_roots VALUES (?,?)', ('p', str(Path(engine.profile) / 'project')))
            c.execute('INSERT INTO remote_control_enrollments VALUES (?)', ('SECRET-REMOTE-TOKEN-12345',))
        with database(engine.home / 'thread_history_1.sqlite') as c:
            c.executescript('CREATE TABLE thread_items (thread_id TEXT, item_json TEXT); CREATE TABLE thread_turns (thread_id TEXT, rollout_byte_offset INTEGER);')
            c.execute('INSERT INTO thread_items VALUES (?,?)', ('one', json.dumps({'text': text})))
            c.execute('INSERT INTO thread_turns VALUES (?,?)', ('one', 1234))

    def append(self, engine, text):
        with (engine.home / 'sessions' / '2026' / 'one.jsonl').open('a', encoding='utf-8') as f:
            f.write('\n' + text)
        with database(engine.home / 'thread_history_1.sqlite') as c:
            c.execute('INSERT INTO thread_items VALUES (?,?)', ('one', json.dumps({'text': text})))

    def test_roundtrip_paths_bytes_and_credentials(self):
        self.init(self.b, 'B original')
        original = (self.a.home / 'sessions/2026/one.jsonl').read_bytes()
        self.a.export(self.store)
        self.b.receive(self.store, True)
        self.assertEqual(original, (self.b.home / 'sessions/2026/one.jsonl').read_bytes())
        with database(self.b.home / 'state_5.sqlite') as c:
            cwd, rollout = c.execute('SELECT cwd,rollout_path FROM threads').fetchone()
            self.assertEqual(Path(cwd), Path(self.b.profile) / 'project')
            self.assertEqual(Path(rollout), self.b.home / 'sessions/2026/one.jsonl')
            self.assertEqual(c.execute('SELECT COUNT(*) FROM remote_control_enrollments').fetchone()[0], 0)
        self.assertEqual((self.b.home / 'auth.json').read_text(), 'LOCAL TOKEN')
        self.assertEqual((self.b.home / 'config.toml').read_text(), 'LOCAL CONFIG')
        self.assertEqual(h.read_json(self.b.home / '.codex-global-state.json')['machine-only'], 'retained')
        self.append(self.b, 'B new turn')
        self.b.export(self.store)
        self.a.receive(self.store)
        self.assertIn('B new turn', (self.a.home / 'sessions/2026/one.jsonl').read_text())

    def test_initial_confirmation_required(self):
        self.a.export(self.store)
        with self.assertRaises(h.HandoffError):
            self.b.receive(self.store)
        self.assertFalse(self.b.home.exists())

    def test_extended_windows_paths_map_to_current_profile(self):
        mappings = [(r'C:\Users\olduser\.codex', r'C:\Users\New User\.codex'),
                    (r'C:\Users\olduser', r'C:\Users\New User')]
        for prefix in ['', '\\\\?\\']:
            self.assertEqual(h.map_path(prefix + r'C:\Users\olduser\.codex\sessions\one.jsonl', mappings),
                             r'C:\Users\New User\.codex\sessions\one.jsonl')
            self.assertEqual(h.map_path(prefix + r'C:\Users\olduser\Documents\project', mappings),
                             r'C:\Users\New User\Documents\project')
        self.assertEqual(h.map_path(r'\\?\C:\Users\olduser2\file', mappings),
                         r'\\?\C:\Users\olduser2\file')
        self.assertEqual(h.map_path(r'C:\Users\old\file', [(r'\\?\C:\Users\old', r'D:\new')]),
                         r'D:\new\file')
        self.assertEqual(h.map_path(r'\\?\UNC\server\share\file', [(r'\\server\share', r'D:\new')]),
                         r'D:\new\file')

    def test_roundtrip_extended_rollout_and_compressed_history(self):
        original = self.a.home/'sessions/2026/one.jsonl'
        compressed = original.with_suffix('.jsonl.zst')
        original.rename(compressed)
        content = compressed.read_bytes()
        with database(self.a.home/'state_5.sqlite') as c:
            c.execute('UPDATE threads SET rollout_path=?,cwd=?',
                      ('\\\\?\\' + str(original), '\\\\?\\' + str(Path(self.a.profile)/'project')))
        self.a.export(self.store)
        self.b.receive(self.store, True)
        with database(self.b.home/'state_5.sqlite') as c:
            cwd, rollout = c.execute('SELECT cwd,rollout_path FROM threads').fetchone()
        self.assertEqual(Path(cwd), Path(self.b.profile)/'project')
        self.assertEqual(Path(rollout), self.b.home/'sessions/2026/one.jsonl')
        self.assertEqual(content, (self.b.home/'sessions/2026/one.jsonl.zst').read_bytes())

    def test_divergence_refused_and_stale_export_refused(self):
        self.a.export(self.store)
        self.b.receive(self.store, True)
        self.append(self.a, 'A diverged')
        self.append(self.b, 'B diverged')
        self.b.export(self.store)
        with self.assertRaises(h.HandoffError):
            self.a.receive(self.store, True)
        with self.assertRaises(h.HandoffError):
            self.a.export(self.store)
        self.assertIn('A diverged', (self.a.home / 'sessions/2026/one.jsonl').read_text())

    def test_same_head_noop(self):
        self.a.export(self.store)
        self.b.receive(self.store, True)
        self.assertEqual(self.b.receive(self.store)[0], 'already')

    def test_corruption_refused_before_mutation(self):
        self.init(self.b, 'B original')
        m = self.a.export(self.store)
        store = h.Store(self.store)
        p = store.object(next(e['sha256'] for e in m['files'] if e['path'].endswith('.jsonl')))
        p.write_bytes(b'CORRUPTED')
        with self.assertRaises(h.HandoffError):
            self.b.receive(self.store, True)
        self.assertIn('B original', (self.b.home / 'sessions/2026/one.jsonl').read_text())

    def test_unknown_database_version_refused(self):
        (self.a.home / 'state_99.sqlite').write_bytes(b'new version')
        with self.assertRaises(h.HandoffError):
            self.a.export(self.store)

    def test_different_schema_refused(self):
        self.init(self.b, 'B original')
        with database(self.b.home / 'state_5.sqlite') as c:
            c.execute('ALTER TABLE threads ADD COLUMN changed TEXT')
        self.a.export(self.store)
        with self.assertRaises(h.HandoffError):
            self.b.receive(self.store, True)

    def test_rollback_restores_before_receive(self):
        self.init(self.b, 'B original')
        self.a.export(self.store)
        self.b.receive(self.store, True)
        self.b.restore()
        self.assertIn('B original', (self.b.home / 'sessions/2026/one.jsonl').read_text())
        self.assertFalse(self.b.state_path.exists())

    def test_failure_after_apply_automatically_rolls_back(self):
        self.init(self.b, 'B original')
        self.a.export(self.store)
        real_apply = self.b.apply
        count = [0]
        def fail_once(*args):
            real_apply(*args)
            count[0] += 1
            if count[0] == 1:
                raise OSError('simulated crash after replacing all files')
        with patch.object(self.b, 'apply', side_effect=fail_once):
            with self.assertRaises(OSError):
                self.b.receive(self.store, True)
        self.assertIn('B original', (self.b.home / 'sessions/2026/one.jsonl').read_text())
        self.assertFalse((self.b.data / 'recovery.json').exists())

    def test_interrupted_operation_recovery(self):
        self.init(self.b, 'B original')
        self.b.make_backup([])
        self.append(self.b, 'partial mutation')
        with self.assertRaises(h.HandoffError):
            self.b.export(self.store)
        self.b.restore()
        self.assertNotIn('partial mutation', (self.b.home / 'sessions/2026/one.jsonl').read_text())

    def test_incremental_objects_reused(self):
        self.a.export(self.store)
        before = {str(p.relative_to(self.store)): p.stat().st_mtime_ns for p in (self.store / 'objects').rglob('*') if p.is_file()}
        self.a.export(self.store)
        after = {str(p.relative_to(self.store)): p.stat().st_mtime_ns for p in (self.store / 'objects').rglob('*') if p.is_file()}
        self.assertEqual(len(after), len(before) + 1)  # only a new manifest
        for k, value in before.items():
            self.assertEqual(value, after[k])

    def test_project_files_and_deletions_backed_up(self):
        project = Path(self.a.profile) / 'project'
        project.mkdir()
        (project / 'model.py').write_text('revision 1')
        (project / 'remove.txt').write_text('keep in backup')
        (project / 'node_modules').mkdir()
        (project / 'node_modules/skip').write_text('dependency')
        self.a.export(self.store, [project])
        self.b.receive(self.store, True)
        target = Path(self.b.profile) / 'project'
        self.assertEqual((target / 'model.py').read_text(), 'revision 1')
        self.assertFalse((target / 'node_modules').exists())
        (project / 'remove.txt').unlink()
        (project / 'model.py').write_text('revision 2')
        self.a.export(self.store, [project])
        self.b.receive(self.store)
        self.assertFalse((target / 'remove.txt').exists())
        self.b.restore()
        self.assertTrue((target / 'remove.txt').exists())
        self.assertEqual((target / 'model.py').read_text(), 'revision 1')

    def test_project_divergence_refused(self):
        project = Path(self.a.profile) / 'project'
        project.mkdir()
        (project / 'file').write_text('original')
        self.a.export(self.store, [project])
        self.b.receive(self.store, True)
        (Path(self.b.profile) / 'project/file').write_text('local change')
        with self.assertRaises(h.HandoffError):
            self.b.receive(self.store)

    def test_path_traversal_and_auth_injection_rejected(self):
        self.a.export(self.store)
        store = h.Store(self.store)
        manifest = store.load()
        for path in ['../auth.json', 'auth.json', 'sessions/../../auth.json', 'C:/Windows/file']:
            m = json.loads(json.dumps(manifest))
            m['files'][0]['path'] = path
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(h.HandoffError):
                    self.b.prepare(store, m, Path(tmp))

    def test_sqlite_backup_includes_committed_wal(self):
        p = self.a.home / 'thread_history_1.sqlite'
        with database(p) as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.execute('INSERT INTO thread_items VALUES (?,?)', ('one', 'committed WAL value'))
            c.commit()
            self.a.export(self.store)
            self.b.receive(self.store, True)
        with database(self.b.home / p.name) as c:
            self.assertIn(('committed WAL value',), c.execute('SELECT item_json FROM thread_items').fetchall())

    def test_remote_credentials_purged_from_exported_db(self):
        m = self.a.export(self.store)
        e = next(e for e in m['files'] if e['path'] == 'state_5.sqlite')
        self.assertNotIn(b'SECRET-REMOTE-TOKEN-12345', h.Store(self.store).object(e['sha256']).read_bytes())

    def test_store_and_project_overlap_refused(self):
        with self.assertRaises(h.HandoffError):
            self.a.export(self.a.home / 'transfer')
        project = Path(self.a.profile) / 'project'
        project.mkdir()
        with self.assertRaises(h.HandoffError):
            self.a.export(project / 'transfer', [project])


class ProcessGuardTests(unittest.TestCase):
    def check(self, rows, paths):
        result = subprocess.CompletedProcess([], 0, stdout=rows.encode('mbcs'))
        with patch.object(h.subprocess, 'run', return_value=result), \
                patch.object(h, 'process_image_path', side_effect=paths):
            h.offline_guard()

    def test_standalone_chatgpt_startup_does_not_block(self):
        for path in [r'C:\Program Files\WindowsApps\OpenAI.ChatGPT-Desktop_1.0_x64__abc\app\ChatGPT.exe',
                     r'C:\Program Files\WindowsApps\OpenAI.ChatGPT_1.0_x64__abc\app\ChatGPT.exe',
                     r'C:\Users\用户\AppData\Local\Programs\ChatGPT\ChatGPT.exe']:
            with self.subTest(path=path):
                self.check('"ChatGPT.exe","12"\n"ChatGPT.exe","13"', [path, path])

    def test_codex_disguised_as_chatgpt_blocks_with_identity(self):
        path = r'C:\Program Files\WindowsApps\OpenAI.Codex_26.1_x64__abc\app\ChatGPT.exe'
        with self.assertRaises(h.HandoffError) as caught:
            self.check('"ChatGPT.exe","12"', [path])
        self.assertIn('Codex 仍在运行', str(caught.exception))
        self.assertIn('PID 12', str(caught.exception))
        self.assertIn(path, str(caught.exception))

    def test_legacy_codex_install_and_case_insensitive_paths(self):
        path = r'C:\Users\user\AppData\Local\OpenAI\CODEX\app\CHATGPT.EXE'
        with self.assertRaisesRegex(h.HandoffError, 'Codex 仍在运行'):
            self.check('"CHATGPT.EXE","12"', [path])

    def test_codex_path_takes_priority_over_chatgpt_folder(self):
        path = r'C:\OpenAI\Codex\ChatGPT\ChatGPT.exe'
        with self.assertRaises(h.HandoffError):
            self.check('"ChatGPT.exe","12"', [path])

    def test_all_codex_process_names_block(self):
        for name in ['Codex.exe', 'codex-cli.exe', 'codex-app.exe',
                     'codex-desktop.exe', 'codex-executor.exe']:
            with self.subTest(name=name), self.assertRaises(h.HandoffError):
                self.check(f'"{name}","12"', [rf'C:\apps\{name}'])

    def test_chatgpt_does_not_hide_active_codex(self):
        with self.assertRaises(h.HandoffError):
            self.check('"ChatGPT.exe","12"\n"codex.exe","13"',
                       [r'C:\apps\ChatGPT\ChatGPT.exe', r'C:\apps\codex.exe'])

    def test_unknown_chatgpt_install_is_explained(self):
        with self.assertRaisesRegex(h.HandoffError, '无法确认'):
            self.check('"ChatGPT.exe","12"', [r'C:\unknown\ChatGPT.exe'])

    def test_unreadable_path_does_not_disable_guard(self):
        with self.assertRaisesRegex(h.HandoffError, '无法读取程序路径'):
            self.check('"ChatGPT.exe","12"', [PermissionError('access denied')])
        with self.assertRaisesRegex(h.HandoffError, 'Codex 仍在运行'):
            self.check('"codex.exe","12"', [PermissionError('access denied')])

    def test_exited_process_is_ignored(self):
        self.check('"ChatGPT.exe","12"\n"codex.exe","13"', [None, None])

    def test_own_binary_sandbox_service_and_empty_list_allowed(self):
        self.check('"CodexHandoff.exe","12"\n"codex-windows-sandbox-service.exe","13"', [])
        self.check('', [])

    def test_process_scan_failure_is_explained(self):
        for error in [OSError('not found'), subprocess.TimeoutExpired('tasklist', 10),
                      subprocess.CalledProcessError(1, 'tasklist')]:
            with self.subTest(error=error), patch.object(h.subprocess, 'run', side_effect=error):
                with self.assertRaisesRegex(h.HandoffError, '无法检查运行中的程序'):
                    h.offline_guard()

    def test_malformed_candidate_is_not_silently_allowed(self):
        with self.assertRaisesRegex(h.HandoffError, '进程检查结果无效'):
            self.check('"ChatGPT.exe","unknown"', [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
