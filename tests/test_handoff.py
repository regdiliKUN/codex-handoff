import contextlib
import importlib.util
import json
from pathlib import Path
import sqlite3
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

    def test_process_guard_and_own_binary(self):
        result = type('Result', (), {'stdout': b'"Codex.exe","12","Console","1","100 K"\r\n'})()
        with patch.object(h.subprocess, 'run', return_value=result):
            with self.assertRaises(h.HandoffError):
                h.offline_guard()
        result.stdout = b'"CodexHandoff.exe","12","Console","1","100 K"\r\n'
        with patch.object(h.subprocess, 'run', return_value=result):
            h.offline_guard()

    def test_store_and_project_overlap_refused(self):
        with self.assertRaises(h.HandoffError):
            self.a.export(self.a.home / 'transfer')
        project = Path(self.a.profile) / 'project'
        project.mkdir()
        with self.assertRaises(h.HandoffError):
            self.a.export(project / 'transfer', [project])


if __name__ == '__main__':
    unittest.main(verbosity=2)
