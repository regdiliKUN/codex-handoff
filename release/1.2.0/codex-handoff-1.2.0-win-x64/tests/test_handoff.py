import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
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

    def test_malformed_global_state_is_refused(self):
        (self.a.home / '.codex-global-state.json').write_text('[]', encoding='utf-8')
        with self.assertRaisesRegex(h.HandoffError, '桌面状态'):
            self.a.export(self.store)

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
        mappings = [(r'C:\Users\xukunhang\.codex', r'C:\Users\XU KUNHANG\.codex'),
                    (r'C:\Users\xukunhang', r'C:\Users\XU KUNHANG')]
        for prefix in ['', '\\\\?\\']:
            self.assertEqual(h.map_path(prefix + r'C:\Users\xukunhang\.codex\sessions\one.jsonl', mappings),
                             r'C:\Users\XU KUNHANG\.codex\sessions\one.jsonl')
            self.assertEqual(h.map_path(prefix + r'C:\Users\xukunhang\Documents\project', mappings),
                             r'C:\Users\XU KUNHANG\Documents\project')
        self.assertEqual(h.map_path(r'\\?\C:\Users\xukunhang2\file', mappings),
                         r'\\?\C:\Users\xukunhang2\file')
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

    def test_json_payload_paths_are_remapped_without_reformatting_other_rows(self):
        project = Path(self.a.profile) / 'project'
        with database(self.a.home / 'state_5.sqlite') as c:
            c.execute('INSERT INTO thread_attachments VALUES (?,?)',
                      (str(project / 'image.png'), json.dumps({'path': str(project / 'image.png')})))
            c.execute('INSERT INTO thread_attachments VALUES (?,?)',
                      ('no-path', '{"x":1}'))
        self.a.export(self.store)
        self.b.receive(self.store, True)
        with database(self.b.home / 'state_5.sqlite') as c:
            rows = dict(c.execute('SELECT identity_key,payload FROM thread_attachments').fetchall())
        self.assertEqual(rows['no-path'], '{"x":1}')
        mapped = json.loads(rows[str(Path(self.b.profile) / 'project' / 'image.png')])
        self.assertEqual(mapped['path'], str(Path(self.b.profile) / 'project' / 'image.png'))

    def test_different_user_version_refused(self):
        self.init(self.b, 'B original')
        with database(self.b.home / 'state_5.sqlite') as c:
            c.execute('PRAGMA user_version=99')
        self.a.export(self.store)
        with self.assertRaisesRegex(h.HandoffError, '结构不一致'):
            self.b.receive(self.store, True)

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
        first = self.a.export(self.store)
        head = h.Store(self.store).head()
        before = {str(p.relative_to(self.store)): p.stat().st_mtime_ns for p in (self.store / 'objects').rglob('*') if p.is_file()}
        with patch.object(self.a, 'capture', side_effect=AssertionError('unchanged export must not scan')):
            second = self.a.export(self.store)
        after = {str(p.relative_to(self.store)): p.stat().st_mtime_ns for p in (self.store / 'objects').rglob('*') if p.is_file()}
        self.assertEqual(second['id'], first['id'])
        self.assertEqual(h.Store(self.store).head(), head)
        self.assertEqual(after, before)

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

    def test_failure_during_copy_restores_previous_snapshot(self):
        self.init(self.b, 'B original')
        self.a.export(self.store)
        real_copy = h.shutil.copyfile
        failed = [False]
        def flaky_copy(src, dst, *args, **kwargs):
            if '.handoff-' in str(dst) and not failed[0]:
                failed[0] = True
                raise OSError('simulated disk failure during apply')
            return real_copy(src, dst, *args, **kwargs)
        with patch.object(h.shutil, 'copyfile', side_effect=flaky_copy):
            with self.assertRaises(OSError):
                self.b.receive(self.store, True)
        self.assertIn('B original', (self.b.home / 'sessions/2026/one.jsonl').read_text())
        self.assertFalse((self.b.data / 'recovery.json').exists())

    def test_receive_does_not_rewrite_unchanged_files(self):
        project = Path(self.a.profile) / 'project'
        project.mkdir(parents=True)
        (project / 'file').write_text('v1')
        self.a.export(self.store, [project])
        self.b.receive(self.store, True)
        session = self.b.home / 'sessions/2026/one.jsonl'
        before = session.stat().st_mtime_ns
        (project / 'file').write_text('v2')
        self.a.export(self.store, [project])
        self.b.receive(self.store)
        self.assertEqual(session.stat().st_mtime_ns, before)
        self.assertEqual((Path(self.b.profile) / 'project/file').read_text(), 'v2')

    def test_prepare_refuses_when_staging_space_is_insufficient(self):
        manifest = self.a.export(self.store)
        store = h.Store(self.store)
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(h.shutil, 'disk_usage', return_value=type('Usage', (), {'free': 0})()):
            with self.assertRaisesRegex(h.HandoffError, '空间不足'):
                self.b.prepare(store, manifest, Path(tmp))

    def test_corrupt_database_check_is_a_handoff_error(self):
        bad = self.root / 'bad.sqlite'
        bad.write_bytes(b'not a database')
        with self.assertRaisesRegex(h.HandoffError, '完整性检查失败'):
            h.database_check(bad)

    def test_remote_credentials_purged_from_both_databases(self):
        with database(self.a.home / 'thread_history_1.sqlite') as c:
            c.execute('CREATE TABLE remote_control_enrollments (token TEXT)')
            c.execute('INSERT INTO remote_control_enrollments VALUES (?)', ('HISTORY-SECRET',))
        m = self.a.export(self.store)
        store = h.Store(self.store)
        for name in ('state_5.sqlite', 'thread_history_1.sqlite'):
            e = next(e for e in m['files'] if e['path'] == name)
            data = store.object(e['sha256']).read_bytes()
            self.assertNotIn(b'REMOTE-TOKEN', data)
            self.assertNotIn(b'HISTORY-SECRET', data)

    def test_remote_credentials_purged_from_exported_db(self):
        m = self.a.export(self.store)
        e = next(e for e in m['files'] if e['path'] == 'state_5.sqlite')
        self.assertNotIn(b'SECRET-REMOTE-TOKEN-12345', h.Store(self.store).object(e['sha256']).read_bytes())

    def test_store_identity_is_stable_under_concurrent_creation(self):
        root = self.root / 'concurrent-store'
        results = []
        errors = []
        def initialize():
            try:
                results.append(h.Store(root).initialize())
            except Exception as exc:  # pragma: no cover - diagnostic path
                errors.append(exc)
        threads = [threading.Thread(target=initialize) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertFalse(errors)
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(h.Store(root).initialize(), results[0])

    def test_store_rejects_directory_and_linked_objects(self):
        store = h.Store(self.root / 'store')
        store.initialize()
        with self.assertRaises(h.HandoffError):
            store.put(self.root)
        target = self.root / 'object-source'
        target.write_text('data', encoding='utf-8')
        sha = store.put(target)
        obj = store.object(sha)
        link = obj.with_name(obj.name + '-link')
        try:
            os.symlink(obj, link)
        except (OSError, NotImplementedError):
            self.skipTest('creating symlinks is not permitted on this machine')
        with self.assertRaises(h.HandoffError):
            store.put(link)

    def test_malformed_recovery_marker_is_explained(self):
        self.a.make_backup([])
        h.atomic_json(self.a.data / 'recovery.json', {'home': str(self.a.home)})
        with self.assertRaisesRegex(h.HandoffError, '恢复标记'):
            self.a.restore()

    def test_failure_after_backup_before_apply_clears_recovery_marker(self):
        self.a.export(self.store)
        real_guard = self.b.guard
        calls = [0]
        def guard():
            calls[0] += 1
            if calls[0] == 3:
                raise h.HandoffError('Codex 在备份后启动')
            return real_guard()
        self.b.guard = guard
        with self.assertRaisesRegex(h.HandoffError, '备份后启动'):
            self.b.receive(self.store, True)
        self.assertFalse((self.b.data / 'recovery.json').exists())
        self.assertFalse(self.b.home.exists())

    def test_progress_reports_every_manifest_file(self):
        calls = []
        self.a.progress = lambda done, total: calls.append((done, total))
        manifest = self.a.export(self.store)
        self.assertTrue(calls)
        self.assertEqual(calls[-1], (len(manifest['files']), len(manifest['files'])))
        self.assertEqual(sorted(set(total for _, total in calls)), [len(manifest['files'])])

    def test_manifest_paths_are_deterministic(self):
        m = self.a.export(self.store)
        codex_paths = [e['path'] for e in m['files'] if e['root'] == 'codex']
        self.assertEqual(codex_paths, sorted(codex_paths, key=str.casefold))
        self.assertEqual(codex_paths, sorted(
            (p.relative_to(self.a.home).as_posix() for p in h.inventory(self.a.home)), key=str.casefold))

    def test_digest_matches_hashlib(self):
        p = self.root / 'digest.bin'
        p.write_bytes(b'abc' * 1000)
        self.assertEqual(h.digest(p), hashlib.sha256(p.read_bytes()).hexdigest())

    def test_absolute_path_length_limit(self):
        with patch.object(h, 'MAX_ABSOLUTE_PATH', 1):
            with self.assertRaisesRegex(h.HandoffError, '路径过长'):
                h.safe_file(self.root, 'file')

    def test_linked_parent_is_refused_even_when_leaf_does_not_exist(self):
        real = self.root / 'real-folder'
        real.mkdir()
        link = self.root / 'linked-folder'
        try:
            os.symlink(real, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('creating symlinks is not permitted on this machine')
        with self.assertRaisesRegex(h.HandoffError, '链接'):
            h.no_link_path(link / 'not-created-yet')

    def test_publish_refuses_linked_snapshot_directory(self):
        manifest = self.a.export(self.store)
        root = self.root / 'linked-store'
        store = h.Store(root)
        store.initialize()
        outside = self.root / 'outside-snapshots'
        outside.mkdir()
        try:
            os.symlink(outside, root / 'snapshots')
        except (OSError, NotImplementedError):
            self.skipTest('creating symlinks is not permitted on this machine')
        with self.assertRaises(h.HandoffError):
            store.publish(manifest, None)

    def test_store_update_during_prepare_aborts_before_mutation(self):
        self.a.export(self.store)
        real_prepare = self.b.prepare
        changed = [False]
        def prepare_then_update(*args, **kwargs):
            result = real_prepare(*args, **kwargs)
            if not changed[0]:
                changed[0] = True
                self.append(self.a, 'changed while preparing')
                self.a.export(self.store)
            return result
        with patch.object(self.b, 'prepare', side_effect=prepare_then_update):
            with self.assertRaisesRegex(h.HandoffError, '更新'):
                self.b.receive(self.store, True)
        self.assertFalse(self.b.home.exists())

    def test_empty_or_corrupt_source_cannot_export(self):
        empty = h.Engine(self.root / 'empty' / '.codex', self.root / 'empty-tool',
                         str(self.root / 'empty'), guard=lambda: None, log=lambda _: None)
        empty.home.mkdir(parents=True)
        with self.assertRaisesRegex(h.HandoffError, '没有可导出'):
            empty.export(self.root / 'empty-store')
        broken = self.a.data / (h.hashlib.sha256(str(self.a.home).casefold().encode()).hexdigest()[:16] + '.json')
        h.atomic_json(broken, {'store_id': 'bad'})
        with self.assertRaisesRegex(h.HandoffError, '状态'):
            self.a.state()

    def test_json_errors_are_actionable_and_bom_is_accepted(self):
        bad = self.root / 'bad.json'
        bad.write_text('{oops', encoding='utf-8')
        with self.assertRaisesRegex(h.HandoffError, '第 1 行'):
            h.read_json(bad)
        missing = self.root / 'missing.json'
        with self.assertRaisesRegex(h.HandoffError, '找不到文件'):
            h.read_json(missing)
        with self.assertRaisesRegex(h.HandoffError, '过大'):
            h.read_json(bad, limit=1)
        bom = self.root / 'bom.json'
        bom.write_text('\ufeff{"ok": true}', encoding='utf-8')
        self.assertEqual(h.read_json(bom), {'ok': True})
        h.atomic_json(self.root / 'written.json', {'中文': '值'})
        self.assertEqual(h.read_json(self.root / 'written.json'), {'中文': '值'})

    def test_path_mapping_drive_roots_and_trailing_slashes(self):
        mappings = [('C:\\', 'D:\\'), ('C:\\Users\\old\\', 'E:\\New\\')]
        self.assertEqual(h.map_path(r'C:\Users\old\file', mappings), r'E:\New\file')
        self.assertEqual(h.map_path(r'C:\Users\old', mappings), r'E:\New')
        self.assertEqual(h.map_path(r'C:\Windows\file', mappings), r'D:\Windows\file')
        self.assertEqual(h.map_path('C:\\', mappings), 'D:\\')
        self.assertEqual(h.map_path(r'c:\windows\file', mappings), r'D:\windows\file')
        self.assertEqual(h.map_path(r'C:\Users\older\file', mappings), r'D:\Users\older\file')

    def test_windows_path_aliases_and_reserved_names_rejected(self):
        for path in ['CON', 'a/con.txt', 'PRN.json', 'COM1', 'LPT9.log',
                     'file.', 'folder /file', 'bad\x01name', 'x/' + 'a' * 256,
                     'a//b', 'a/./b', 'a/../b']:
            with self.subTest(path=path):
                with self.assertRaises(h.HandoffError):
                    h.relative_parts(path)
        self.assertEqual(h.relative_parts('sessions/2026/ok.jsonl'),
                         ('sessions', '2026', 'ok.jsonl'))

    def test_malformed_manifest_rejected(self):
        manifest = self.a.export(self.store)
        cases = []
        for change in [
            lambda m: m.update(files='not-a-list'),
            lambda m: m.update(id='not-an-id'),
            lambda m: m.update(roots={'codex': self.a.home, 'p0': str(self.a.profile)}),
            lambda m: m['files'][0].update(root='unknown'),
            lambda m: m['files'][0].update(path='../auth.json'),
            lambda m: m['files'][0].update(size=True),
            lambda m: m['files'][0].update(sha256='0' * 63),
            lambda m: m['files'].append(dict(m['files'][0])),
        ]:
            candidate = json.loads(json.dumps(manifest))
            change(candidate)
            cases.append(candidate)
        for candidate in cases:
            with self.subTest(candidate=candidate):
                with self.assertRaises(h.HandoffError):
                    h.validate_manifest(candidate)
        self.assertEqual(h.validate_manifest(manifest)['id'], manifest['id'])
        with self.assertRaises(h.HandoffError):
            h.validate_manifest(manifest, 'f' * 32)

    def test_snapshot_with_malformed_manifest_is_refused(self):
        manifest = self.a.export(self.store)
        store = h.Store(self.store)
        bad = json.loads(json.dumps(manifest))
        bad['files'] = 'broken'
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'manifest.json'
            h.atomic_json(p, bad)
            sha = store.put(p)
        h.atomic_json(self.store / 'latest.json', {'id': manifest['id'], 'sha256': sha})
        with self.assertRaises(h.HandoffError):
            store.load()

    def test_project_boundaries_and_case_insensitive_duplicates(self):
        project = Path(self.a.profile) / 'project'
        project.mkdir(parents=True)
        with self.assertRaisesRegex(h.HandoffError, '系统目录'):
            self.a.export(self.store, [self.root])
        with self.assertRaisesRegex(h.HandoffError, '工具数据'):
            self.a.export(self.store, [self.a.data / 'inside'])
        store = h.Store(self.store)
        store.initialize()
        with self.assertRaisesRegex(h.HandoffError, '重复'):
            self.a.validate_projects([project, str(project).upper()], store)

    def test_store_and_project_overlap_refused(self):
        with self.assertRaises(h.HandoffError):
            self.a.export(self.a.home / 'transfer')
        project = Path(self.a.profile) / 'project'
        project.mkdir()
        with self.assertRaises(h.HandoffError):
            self.a.export(project / 'transfer', [project])

    def test_manifest_tool_version_and_file_order(self):
        m = self.a.export(self.store)
        self.assertEqual(m['tool'], h.VERSION)
        self.assertEqual(m['files'], sorted(
            m['files'],
            key=lambda e: (0 if e['root'] == 'codex' else int(e['root'][1:]) + 1,
                           e['path'].casefold())))

    def test_manifest_metadata_is_strictly_validated(self):
        manifest = self.a.export(self.store)
        changes = [
            (lambda m: m.update(created='not-a-time'), '导出时间'),
            (lambda m: m.update(raw_backup='yes'), 'raw_backup'),
            (lambda m: m.update(profile='relative/profile'), '用户目录'),
            (lambda m: m['roots'].update(codex='relative/codex'), 'Codex 目录'),
            (lambda m: m.update(parent=m['id']), '自身'),
        ]
        for change, reason in changes:
            candidate = json.loads(json.dumps(manifest))
            change(candidate)
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(h.HandoffError, reason):
                    h.validate_manifest(candidate)

    def test_store_publish_rejects_wrong_parent(self):
        self.a.export(self.store)
        store = h.Store(self.store)
        head = store.head()
        manifest = self.a.capture(store, [], parent='f' * 32)
        with self.assertRaisesRegex(h.HandoffError, '上一版本'):
            store.publish(manifest, head)
        self.assertEqual(store.head(), head)

    def test_store_publish_rejects_reused_snapshot_id_with_other_content(self):
        self.a.export(self.store)
        store = h.Store(self.store)
        head = store.head()
        manifest = self.a.capture(store, [], parent=head['id'])
        snapshot = store.root / 'snapshots' / (manifest['id'] + '.json')
        h.atomic_json(snapshot, {'id': manifest['id'], 'sha256': '0' * 64})
        with self.assertRaisesRegex(h.HandoffError, '占用'):
            store.publish(manifest, head)

    def test_apply_prunes_stale_empty_directories(self):
        project = Path(self.a.profile) / 'project'
        (project / 'old' / 'nested').mkdir(parents=True)
        (project / 'old' / 'nested' / 'file').write_text('v1')
        (project / 'keep').write_text('keep')
        self.a.export(self.store, [project])
        self.b.receive(self.store, True)
        target = Path(self.b.profile) / 'project'
        self.assertTrue((target / 'old' / 'nested').is_dir())
        (project / 'old' / 'nested' / 'file').unlink()
        (project / 'old' / 'nested').rmdir()
        (project / 'old').rmdir()
        self.a.export(self.store, [project])
        self.b.receive(self.store)
        self.assertFalse((target / 'old').exists())
        self.assertTrue(target.is_dir())
        self.assertEqual((target / 'keep').read_text(), 'keep')

    def test_apply_requires_space_only_for_changed_files(self):
        project = Path(self.a.profile) / 'project'
        project.mkdir()
        (project / 'file').write_text('v1')
        manifest = self.a.export(self.store, [project])
        self.b.receive(self.store, True)
        store = h.Store(self.store)
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            dests = self.b.prepare(store, manifest, stage)
            with patch.object(h.shutil, 'disk_usage',
                              return_value=type('Usage', (), {'free': 0})()):
                self.b.apply(manifest, stage, dests)

    def test_inventory_rejects_directory_where_a_database_is_expected(self):
        other = self.engine('C')
        (other.home / 'sessions').mkdir(parents=True)
        (other.home / 'sessions' / 'one.jsonl').write_text('{}', encoding='utf-8')
        (other.home / 'state_5.sqlite').mkdir()
        with self.assertRaisesRegex(h.HandoffError, '不是普通文件'):
            h.inventory(other.home)

    def test_store_rejects_symlinked_root(self):
        real = self.root / 'real-store'
        real.mkdir()
        link = self.root / 'linked-store-root'
        try:
            os.symlink(real, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('creating symlinks is not permitted on this machine')
        with self.assertRaisesRegex(h.HandoffError, '链接'):
            h.Store(link)

    def test_digest_missing_file_is_explained(self):
        with self.assertRaisesRegex(h.HandoffError, '无法读取文件'):
            h.digest(self.root / 'missing.bin')

    def test_atomic_json_rejects_unserializable_data_cleanly(self):
        target = self.root / 'bad-output.json'
        with self.assertRaisesRegex(h.HandoffError, '无法序列化'):
            h.atomic_json(target, {'bad': {1, 2}})
        self.assertFalse(target.exists())
        self.assertEqual(list(target.parent.glob(target.name + '.*')), [])

    def test_map_tree_depth_limit(self):
        value = {}
        cursor = value
        for _ in range(205):
            cursor['x'] = {}
            cursor = cursor['x']
        with self.assertRaisesRegex(h.HandoffError, '嵌套'):
            h.map_tree(value, [])

    def test_merge_global_state_names_disallowed_keys(self):
        incoming = self.root / 'incoming-state.json'
        h.atomic_json(incoming, {'machine-only': True})
        with self.assertRaisesRegex(h.HandoffError, 'machine-only'):
            h.merge_global_state(incoming, self.root / 'target-state.json', [])

    def test_duplicate_mapped_project_destinations_rejected(self):
        project = Path(self.a.profile) / 'project'
        project.mkdir()
        manifest = self.a.export(self.store, [project])
        manifest['roots']['p1'] = '\\\\?\\' + str(project)
        with self.assertRaisesRegex(h.HandoffError, '同一位置'):
            self.b.destinations(manifest)

    def test_remap_database_handles_many_rows(self):
        path = self.root / 'many-rows.sqlite'
        with database(path) as c:
            c.execute('CREATE TABLE thread_attachments (identity_key TEXT, payload TEXT)')
            c.executemany('INSERT INTO thread_attachments VALUES (?,?)',
                          [(f'C:\\old\\p\\{i}', json.dumps({'path': f'C:\\old\\p\\{i}'}))
                           for i in range(2500)])
        h.remap_database(path, [('C:\\old', 'D:\\new')])
        with database(path) as c:
            rows = c.execute('SELECT identity_key,payload FROM thread_attachments').fetchall()
        self.assertEqual(len(rows), 2500)
        self.assertEqual(rows[0][0], 'D:\\new\\p\\0')
        self.assertEqual(json.loads(rows[-1][1])['path'], 'D:\\new\\p\\2499')

    def test_verify_store_reports_corruption(self):
        manifest = self.a.export(self.store)
        result = h.verify_store(self.store)
        self.assertEqual(result['files'], len(manifest['files']))
        self.assertGreater(result['bytes'], 0)
        store = h.Store(self.store)
        obj = store.object(manifest['files'][0]['sha256'])
        original = obj.read_bytes()
        obj.write_bytes(b'broken')
        try:
            with self.assertRaisesRegex(h.HandoffError, '校验失败'):
                h.verify_store(self.store)
        finally:
            obj.write_bytes(original)

    def test_diagnose_reports_state_problems(self):
        result = h.diagnose(self.a.home, self.a.data)
        self.assertEqual(result['version'], h.VERSION)
        self.assertEqual(result['state'], 'empty')
        self.assertFalse(result['recovery_pending'])
        h.atomic_json(self.a.state_path, {'store_id': 'bad'})
        result = h.diagnose(self.a.home, self.a.data)
        self.assertEqual(result['state'], 'error')
        self.assertIn('状态', result['state_error'])

    def test_cli_version_self_test_and_diagnose(self):
        for args, key in [(["--version"], None), (["--self-test"], "sqlite")]:
            result = subprocess.run([sys.executable, str(SOURCE), *args], capture_output=True,
                                    text=True, encoding='utf-8', timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            if key:
                self.assertIn(key, result.stdout)
            else:
                self.assertEqual(result.stdout.strip(), h.VERSION)
        output = self.root / 'diagnose.json'
        result = subprocess.run([sys.executable, str(SOURCE), '--diagnose', str(output)],
                                capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(h.read_json(output)['version'], h.VERSION)

    def test_full_project_cycle_with_snapshot_verification(self):
        project = Path(self.a.profile) / 'project'
        (project / 'nested').mkdir(parents=True)
        (project / 'nested' / 'model.py').write_text('revision 1', encoding='utf-8')
        (project / 'remove.txt').write_text('remove me', encoding='utf-8')
        (project / 'binary.bin').write_bytes(bytes(range(256)))
        self.a.export(self.store, [project])
        self.b.receive(self.store, True)
        target = Path(self.b.profile) / 'project'
        self.assertEqual((target / 'nested' / 'model.py').read_text(encoding='utf-8'), 'revision 1')
        self.assertEqual((target / 'binary.bin').read_bytes(), bytes(range(256)))

        (target / 'remove.txt').unlink()
        (target / 'nested' / 'model.py').write_text('revision 2', encoding='utf-8')
        (target / 'nested' / 'new.txt').write_text('from B', encoding='utf-8')
        self.append(self.b, 'B turn after project edit')
        self.b.export(self.store, [target])
        verified = h.verify_store(self.store)
        self.assertGreater(verified['files'], 0)
        self.a.receive(self.store)
        self.assertFalse((project / 'remove.txt').exists())
        self.assertEqual((project / 'nested' / 'model.py').read_text(encoding='utf-8'), 'revision 2')
        self.assertEqual((project / 'nested' / 'new.txt').read_text(encoding='utf-8'), 'from B')
        self.assertIn('B turn after project edit', (self.a.home / 'sessions/2026/one.jsonl').read_text(encoding='utf-8'))

    def test_manifest_root_and_capture_limits_are_enforced_early(self):
        manifest = self.a.export(self.store)
        with patch.object(h, 'MAX_MANIFEST_ROOTS', 1):
            candidate = json.loads(json.dumps(manifest))
            candidate['roots']['p0'] = str(Path(self.a.profile) / 'a')
            candidate['roots']['p1'] = str(Path(self.a.profile) / 'b')
            with self.assertRaisesRegex(h.HandoffError, '项目数量'):
                h.validate_manifest(candidate)
        self.append(self.a, 'changed for limit tests')
        with patch.object(h, 'MAX_MANIFEST_FILES', 1):
            with self.assertRaisesRegex(h.HandoffError, '文件数量'):
                self.a.export(self.store)
        with patch.object(h, 'MAX_MANIFEST_BYTES', 1):
            with self.assertRaisesRegex(h.HandoffError, '总大小'):
                self.a.export(self.store)

    def test_project_list_type_and_none_are_handled(self):
        self.a.export(self.store)
        project = Path(self.a.profile) / 'project'
        project.mkdir()
        store = h.Store(self.store)
        with self.assertRaisesRegex(h.HandoffError, '项目列表'):
            self.a.validate_projects(str(project), store)
        self.assertEqual(self.a.validate_projects(None, store), [])
        self.assertEqual(self.a.validate_projects((), store), [])


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

    def test_malformed_csv_is_reported(self):
        with self.assertRaisesRegex(h.HandoffError, '无法解析'):
            self.check('"ChatGPT.exe","12', [])

    def test_codex_tui_name_blocks(self):
        with self.assertRaisesRegex(h.HandoffError, 'Codex 仍在运行'):
            self.check('"codex-tui.exe","12"', [r'C:\apps\codex-tui.exe'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
