import importlib.util
from pathlib import Path
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
        self.release = None
        self.guard_error = None
        self.export_error = None

    def state(self):
        return {}

    def guard(self):
        self.guard_calls += 1
        if self.guard_error:
            raise api.HandoffError(self.guard_error)

    def export(self, path, projects):
        self.export_calls += 1
        self.log('读取：sessions/test.jsonl')
        if self.release:
            self.release.wait(3)
        if self.export_error:
            raise api.HandoffError(self.export_error)
        return {'files': ['file1', 'file2']}


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


if __name__ == '__main__':
    unittest.main(verbosity=2)
