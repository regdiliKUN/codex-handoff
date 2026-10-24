import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

PACKAGE = Path(__file__).parents[1]
sys.path.insert(0, str(PACKAGE))
import handoff as api
import handoff_ui as ui


class FakeEngine:
    def __init__(self, base):
        self.home, self.data = base / '.codex', base / 'tool'
        self.guard_calls = 0
        self.export_calls = 0
        self.receive_calls = 0
        self.restore_calls = 0
        self.release = None
        self.guard_error = None
        self.export_error = None
        self.receive_error = None
        self.restore_error = None
        self.state_error = None
        self.state_error_after_receive = None
        self.received = False
        self.receive_state = 'received'

    def state(self):
        if self.state_error or (self.received and self.state_error_after_receive):
            raise api.HandoffError(self.state_error_after_receive if self.received else self.state_error)
        return {}

    def guard(self):
        self.guard_calls += 1
        if self.guard_error:
            raise api.HandoffError(self.guard_error)

    def export(self, path, projects):
        self.export_calls += 1
        self.export_path = path
        self.log('读取：sessions/test.jsonl')
        if self.release:
            self.release.wait(3)
        if self.export_error:
            raise api.HandoffError(self.export_error)
        return {'files': ['file1', 'file2']}

    def destinations(self, manifest):
        return {'codex': self.home}

    def receive(self, path, allow_initial_replace=False):
        self.receive_calls += 1
        self.receive_path = path
        self.receive_allow = allow_initial_replace
        self.received = True
        if self.receive_error:
            raise api.HandoffError(self.receive_error)
        return self.receive_state, {'files': []}

    def restore(self):
        self.restore_calls += 1
        if self.restore_error:
            raise api.HandoffError(self.restore_error)


class InterfaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ui-fixture-', dir=Path(__file__).parent)
        self.base = Path(self.temp.name)
        self.root = tk.Tk()
        self.root.withdraw()
        self.engine = FakeEngine(self.base)
        self.app = ui.HandoffApp(self.root, api, self.engine)

    def tearDown(self):
        if self.engine.release:
            self.engine.release.set()
        self.app.busy = False
        self.root.update()
        self.app.close()
        self.temp.cleanup()

    def drain(self):
        deadline = time.monotonic() + 4
        while self.app.busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.root.update()
        self.assertFalse(self.app.busy)

    def publish_empty_store(self):
        store = api.Store(self.base / 'transfer')
        store.initialize()
        manifest = {
            'format': 1, 'tool': api.VERSION, 'id': 'a' * 32, 'parent': None,
            'created': '2026-10-22T00:00:00+00:00', 'computer': 'A',
            'profile': str(self.base), 'roots': {'codex': str(self.engine.home)},
            'files': [], 'raw_backup': False,
        }
        store.publish(manifest, None)
        return store

    def test_primary_action_large_blue_and_pinned(self):
        button = self.app.start_button
        self.assertEqual(button.cget('text'), '开始导出聊天  →')
        self.assertEqual(button.cget('bg'), ui.BLUE)
        self.assertGreaterEqual(int(button.cget('pady')), 14)
        self.assertEqual(button.pack_info()['fill'], 'x')
        self.assertEqual(button.master.pack_info()['side'], 'bottom')
        self.app.toggle_projects()
        self.app.toggle_details()
        self.root.geometry('900x680')
        self.root.update_idletasks()
        self.assertEqual(button.master.pack_info()['side'], 'bottom')

    def test_direction_updates_button_and_project_controls(self):
        self.app.mode.set('receive')
        self.assertEqual(self.app.start_button.cget('text'), '开始接收聊天  →')
        self.assertEqual(str(self.app.add_button.cget('state')), 'disabled')
        self.assertIn('接收后', self.app.next_hint.cget('text'))
        self.app.mode.set('export')
        self.assertEqual(str(self.app.add_button.cget('state')), 'normal')

    def test_missing_folder_explained_without_disk_writes(self):
        self.app.start_button.invoke()
        self.assertIn('选择中转文件夹', self.app.status_title.cget('text'))
        self.assertEqual(self.app.store_entry.cget('highlightbackground'), ui.RED)
        self.assertEqual(self.engine.guard_calls, 0)
        self.assertFalse(self.engine.data.exists())

    def test_running_codex_shows_inline_reason(self):
        self.app.store.set(str(self.base / 'transfer'))
        self.engine.guard_error = '请先退出 Codex。'
        self.app.start_button.invoke()
        self.assertEqual(self.app.status_title.cget('text'), '暂时无法开始')
        self.assertIn('退出 Codex', self.app.status_detail.cget('text'))
        self.assertEqual(self.engine.export_calls, 0)
        self.assertTrue(self.app.details_open)
        self.assertIn('退出 Codex', self.app.log.get('1.0', 'end'))

    def test_keyboard_shortcuts_switch_direction(self):
        self.app.set_mode('receive')
        self.assertEqual(self.app.mode.get(), 'receive')
        self.app.set_mode('export')
        self.assertEqual(self.app.mode.get(), 'export')
        self.assertTrue(self.root.bind_all('<Alt-Key-2>'))

    def test_store_path_is_normalized_before_export(self):
        target = self.base / 'transfer'
        self.app.store.set('  "%s"  ' % target)
        self.app.start_button.invoke()
        self.drain()
        self.assertEqual(Path(self.engine.export_path), target.resolve())

    def test_add_project_validates_nested_and_duplicate_paths(self):
        project = self.base / 'project'
        project.mkdir()
        child = project / 'child'
        child.mkdir()
        with patch.object(ui.filedialog, 'askdirectory', return_value=str(project)):
            self.app.add_project()
        self.assertEqual(len(self.app.projects), 1)
        with patch.object(ui.filedialog, 'askdirectory', return_value=str(child)):
            self.app.add_project()
        self.assertEqual(len(self.app.projects), 1)
        self.assertIn('重复', self.app.project_summary.cget('text'))
        with patch.object(ui.filedialog, 'askdirectory', return_value=str(self.engine.home)):
            self.app.add_project()
        self.assertEqual(len(self.app.projects), 1)

    def test_corrupt_settings_fall_back_and_mode_is_remembered(self):
        settings = self.engine.data / 'settings.json'
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text('{broken', encoding='utf-8')
        self.app.close()
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = ui.HandoffApp(self.root, api, self.engine)
        self.assertTrue(self.app.details_open)
        self.assertIn('设置', self.app.log.get('1.0', 'end'))
        settings.unlink()
        settings.write_text(json.dumps({'mode': 'receive'}), encoding='utf-8')
        self.app.close()
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = ui.HandoffApp(self.root, api, self.engine)
        self.assertEqual(self.app.mode.get(), 'receive')

    def test_long_error_is_not_truncated(self):
        self.app.store.set(str(self.base / 'transfer'))
        message = '很长的错误原因：' + ('路径' * 200)
        self.engine.guard_error = message
        self.app.start_button.invoke()
        self.assertEqual(self.app.status_detail.cget('text'), message)

    def test_busy_locks_controls_then_success_is_clear(self):
        self.app.store.set(str(self.base / 'transfer'))
        self.engine.release = threading.Event()
        self.app.start_button.invoke()
        self.assertTrue(self.app.busy)
        self.assertEqual(str(self.app.store_entry.cget('state')), 'disabled')
        self.assertEqual(str(self.app.cards['receive'][4].cget('state')), 'disabled')
        self.assertIn('正在导出', self.app.start_button.cget('text'))
        self.engine.release.set()
        self.drain()
        self.assertEqual(self.engine.export_calls, 1)
        self.assertIn('导出完成', self.app.status_title.cget('text'))
        self.assertIn('同步完成', self.app.status_detail.cget('text'))
        self.assertEqual(self.app.status_title.cget('fg'), ui.GREEN)
        self.assertEqual(str(self.app.start_button.cget('state')), 'normal')
        self.assertEqual(float(self.app.progress['value']), 100)

    def test_failure_unlocks_controls_and_reveals_details(self):
        self.app.store.set(str(self.base / 'transfer'))
        self.engine.export_error = '模拟磁盘空间不足'
        self.app.start_button.invoke()
        self.drain()
        self.assertEqual(self.app.status_title.cget('text'), '交接未完成')
        self.assertIn('磁盘空间不足', self.app.status_detail.cget('text'))
        self.assertTrue(self.app.details_open)
        self.assertEqual(str(self.app.store_entry.cget('state')), 'normal')

    def test_settings_save_failure_does_not_block_export(self):
        self.app.store.set(str(self.base / 'transfer'))
        with patch.object(api, 'atomic_json', side_effect=api.HandoffError('磁盘只读')):
            self.app.start_button.invoke()
            self.drain()
        self.assertEqual(self.engine.export_calls, 1)
        self.assertIn('设置未能保存', self.app.log.get('1.0', 'end'))
        self.assertIn('导出完成', self.app.status_title.cget('text'))

    def test_corrupt_state_uses_saved_projects(self):
        project = self.base / 'saved-project'
        project.mkdir()
        settings = self.engine.data / 'settings.json'
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps({'projects': [str(project)]}), encoding='utf-8')
        self.engine.state_error = '状态文件损坏'
        self.app.close()
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = ui.HandoffApp(self.root, api, self.engine)
        self.assertEqual(self.app.projects, [str(project)])
        self.assertIn('状态', self.app.log.get('1.0', 'end'))
        self.assertTrue(self.app.details_open)

    def test_normalize_store_path_rejects_invalid_and_expands_values(self):
        target = self.base / 'normalized-store'
        target.mkdir()
        with self.assertRaisesRegex(ValueError, '选择中转'):
            ui.normalize_store_path('   ')
        with self.assertRaisesRegex(ValueError, '完整'):
            ui.normalize_store_path('relative\\folder')
        file = self.base / 'not-a-folder'
        file.write_text('x', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '不是文件夹'):
            ui.normalize_store_path(str(file))
        quoted = ui.normalize_store_path('  "%s"  ' % target)
        self.assertEqual(Path(quoted), target.resolve())
        with patch.dict(os.environ, {'CODEX_HANDOFF_TEST_STORE': str(target)}):
            self.assertEqual(Path(ui.normalize_store_path('%CODEX_HANDOFF_TEST_STORE%')),
                             target.resolve())

    def test_add_project_normalizes_quoted_store_for_overlap_check(self):
        project = self.base / 'project'
        project.mkdir()
        self.app.store.set('"%s"' % self.base)
        with patch.object(ui.filedialog, 'askdirectory', return_value=str(project)):
            self.app.add_project()
        self.assertEqual(self.app.projects, [])
        self.assertIn('中转文件夹重叠', self.app.project_summary.cget('text'))

    def test_missing_folder_hint_resets_after_typing(self):
        self.app.start_button.invoke()
        self.assertEqual(self.app.folder_hint.cget('fg'), ui.RED)
        self.app.store.set(str(self.base / 'transfer'))
        self.assertEqual(self.app.folder_hint.cget('fg'), ui.MUTED)

    def test_receive_confirmation_cancel_and_accept(self):
        self.publish_empty_store()
        self.app.store.set(str(self.base / 'transfer'))
        self.app.mode.set('receive')
        with patch.object(ui.messagebox, 'askyesno', return_value=False):
            self.app.start_button.invoke()
        self.assertEqual(self.engine.receive_calls, 0)
        self.assertIn('已取消', self.app.status_title.cget('text'))
        with patch.object(ui.messagebox, 'askyesno', return_value=True):
            self.app.start_button.invoke()
            self.drain()
        self.assertEqual(self.engine.receive_calls, 1)
        self.assertTrue(self.engine.receive_allow)
        self.assertIn('聊天已接收', self.app.status_title.cget('text'))

    def test_receive_success_survives_state_refresh_failure(self):
        self.publish_empty_store()
        self.app.store.set(str(self.base / 'transfer'))
        self.app.mode.set('receive')
        self.engine.state_error_after_receive = '状态暂时无法读取'
        with patch.object(ui.messagebox, 'askyesno', return_value=True):
            self.app.start_button.invoke()
            self.drain()
        self.assertIn('聊天已接收', self.app.status_title.cget('text'))
        self.assertIn('读取项目列表失败', self.app.log.get('1.0', 'end'))

    def test_restore_confirmation_and_success(self):
        with patch.object(ui.messagebox, 'askyesno', return_value=False):
            self.app.start('restore')
        self.assertEqual(self.engine.restore_calls, 0)
        with patch.object(ui.messagebox, 'askyesno', return_value=True):
            self.app.start('restore')
            self.drain()
        self.assertEqual(self.engine.restore_calls, 1)
        self.assertIn('备份已恢复', self.app.status_title.cget('text'))

    def test_close_busy_and_idle(self):
        self.app.busy = True
        with patch.object(ui.messagebox, 'showinfo') as showinfo:
            self.app.close()
        showinfo.assert_called_once()
        self.assertFalse(self.app.closed)
        self.app.busy = False
        with patch.object(self.root, 'destroy') as destroy, \
                patch.object(self.root, 'after_cancel') as after_cancel:
            self.app.close()
        self.assertTrue(self.app.closed)
        destroy.assert_called_once()
        after_cancel.assert_called_once()
        self.app.closed = False

    def test_progress_event_updates_determinate_bar(self):
        self.app.store.set(str(self.base / 'transfer'))
        self.engine.release = threading.Event()
        self.app.start_button.invoke()
        self.app.events.put(('progress', (3, 10)))
        self.app.poll()
        self.assertEqual(str(self.app.progress.cget('mode')), 'determinate')
        self.assertEqual(float(self.app.progress.cget('value')), 3)
        self.assertIn('30%', self.app.status_detail.cget('text'))
        self.engine.release.set()
        self.drain()

    def test_keyboard_start_ignores_focused_button_and_text(self):
        with patch.object(self.root, 'focus_get', return_value=self.app.start_button):
            self.assertIsNone(self.app.keyboard_start())
        with patch.object(self.root, 'focus_get', return_value=self.app.log):
            self.assertIsNone(self.app.keyboard_start())
        self.assertEqual(self.engine.export_calls, 0)

    def test_copy_log_and_layout_report(self):
        self.app.append_log('第一行日志')
        with patch.object(self.root, 'clipboard_clear') as clear, \
                patch.object(self.root, 'clipboard_append') as append, \
                patch.object(self.root, 'update_idletasks'):
            self.app.copy_log()
        clear.assert_called_once()
        self.assertIn('第一行日志', append.call_args[0][0])
        self.assertIn('剪贴板', self.app.copy_hint.cget('text'))
        report = self.app.report()
        self.assertEqual(report['version'], api.VERSION)
        self.assertEqual(report['mode'], 'export')
        self.assertIn('handoff', report['title'].lower().replace('交接助手', 'handoff'))

    def test_human_size_formatting(self):
        self.assertEqual(ui.human_size(0), '0 B')
        self.assertEqual(ui.human_size(1024), '1.0 KiB')
        self.assertEqual(ui.human_size(5 * 1024 ** 3), '5.0 GiB')
        self.assertEqual(ui.path_preview(['a', 'b']), 'a\nb')
        preview = ui.path_preview([str(i) for i in range(10)], limit=3)
        self.assertEqual(preview.count('\n'), 3)
        self.assertIn('另有 7 个', preview)

    def test_cli_ui_smoke_report(self):
        output = self.base / 'ui-smoke.json'
        result = subprocess.run([sys.executable, str(PACKAGE / 'handoff.py'),
                                 '--ui-smoke-test', str(output)],
                                capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = api.read_json(output)
        self.assertEqual(report['version'], api.VERSION)
        self.assertTrue(report['primary_action_pinned_to_footer'])
        self.assertEqual(sorted(report['directions']), ['export', 'receive'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
