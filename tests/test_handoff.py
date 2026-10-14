from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from onyx import handoff


class HerdrHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.folder = self.home / 'a quoted project'
        self.folder.mkdir()
        self.router = self.home / '.local/bin/cx-open-in-herdr'
        self.router.parent.mkdir(parents=True)
        self.router.write_text('#!/bin/sh\nexit 0\n')
        for mocker in [patch.object(Path, 'home', return_value=self.home),
                       patch.object(handoff, 'find_claude', return_value='/tmp/claude'),
                       patch.object(handoff, 'find_codex', return_value='/tmp/codex'),
                       patch.object(handoff, '_find_terminal', return_value=('/tmp/Ghostty.app', 'ghostty'))]:
            mocker.start()
            self.addCleanup(mocker.stop)

    def test_claude_uses_shared_router_and_preserves_folder_and_prompt(self):
        with patch.object(handoff.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run, \
                patch.object(handoff.subprocess, 'Popen') as popen:
            handoff.open_in_provider('claude', self.folder, 'Quoted "prompt"\nwith a second line')
        popen.assert_not_called()
        args = run.call_args.args[0]
        self.assertEqual(args[0], str(self.router))
        self.assertEqual(args[args.index('--cwd') + 1], str(self.folder))
        script = Path(args[args.index('--launch') + 1])
        self.assertIn('--strict-mcp-config', script.read_text())
        self.assertEqual((script.parent / 'prompt.txt').read_text(), 'Quoted "prompt"\nwith a second line')

    def test_codex_keeps_existing_handoff(self):
        with patch.object(handoff.subprocess, 'run') as run, patch.object(handoff.subprocess, 'Popen') as popen:
            handoff.open_in_provider('codex', self.folder, 'hello')
        run.assert_not_called()
        popen.assert_called_once()

    def test_absent_router_keeps_standalone_terminal_support(self):
        self.router.unlink()
        with patch.object(handoff.subprocess, 'run') as run, patch.object(handoff.subprocess, 'Popen') as popen:
            handoff.open_in_provider('claude', self.folder, 'hello')
        run.assert_not_called()
        popen.assert_called_once()

    def test_failed_herdr_handoff_does_not_launch_claude_twice(self):
        with patch.object(handoff.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'routing failed')), \
                patch.object(handoff.subprocess, 'Popen') as popen:
            with self.assertRaisesRegex(RuntimeError, 'routing failed'):
                handoff.open_in_provider('claude', self.folder, 'hello')
        popen.assert_not_called()
