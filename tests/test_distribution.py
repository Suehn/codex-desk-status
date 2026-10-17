"""Exercise distribution boundaries using isolated folders and generated ZIPs."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import package

spec = importlib.util.spec_from_file_location('desk_install', ROOT / 'scripts/install.py')
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)


class DistributionTest(unittest.TestCase):
    def test_install_keeps_other_plugins_and_uninstall_keeps_marketplace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = root / '.agents/plugins/marketplace.json'
            catalog.parent.mkdir(parents=True)
            other = {'name': 'other', 'source': './other'}
            catalog.write_text(json.dumps({'name': 'existing', 'plugins': [other]}))
            self.assertEqual(install.stage(tmp, 'unused', 'python'), 'existing')
            config = json.loads((root / 'plugins/desk-status/mcp.json').read_text())
            self.assertEqual(config['mcpServers']['desk-status']['command'], 'python')
            self.assertIn(other, json.loads(catalog.read_text())['plugins'])
            self.assertEqual(install.unstage(tmp), ('existing', False))
            self.assertEqual(json.loads(catalog.read_text())['plugins'], [other])

    def test_invalid_interpreter_does_not_create_a_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            for command in ['/opt/homebrew/bin/python3', '../python', 'python -B', 'C:\\Python\\python.exe']:
                with self.assertRaises(SystemExit):
                    install.stage(tmp, 'test', command)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_destination_cannot_contain_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / 'plugins/desk-status/source'
            nested.mkdir(parents=True)
            marker = nested / 'keep.txt'
            marker.write_text('keep')
            with patch.object(install, 'ROOT', str(nested)):
                with self.assertRaises(SystemExit):
                    install.stage(tmp, 'test')
            self.assertEqual(marker.read_text(), 'keep')

    def test_runtime_zip_installs_without_source_build_tools(self):
        files = package.expand(package.PLUGIN_FILES)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / 'plugin.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                for rel in files:
                    z.write(ROOT / rel, 'desk-status/' + rel)
            with zipfile.ZipFile(archive) as z:
                z.extractall(root / 'unpacked')
            plugin = root / 'unpacked/desk-status'
            self.assertFalse((plugin / 'scripts/build_ui.py').exists())
            result = subprocess.run(
                [sys.executable, '-B', str(plugin / 'scripts/install.py'), '--marketplace', str(root / 'market')],
                capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root / 'market/plugins/desk-status/ui/dist/panel.html').is_file())

    def test_repo_marketplace_points_at_packaged_manifest(self):
        data = json.loads((ROOT / '.agents/plugins/marketplace.json').read_text())
        self.assertEqual(data['name'], 'codex-desk-status')
        for plugin in data['plugins']:
            target = ROOT / plugin['source']['path'] / 'plugin.json'
            self.assertEqual(json.loads(target.read_text())['name'], plugin['name'])
        files = package.expand(package.SOURCE_FILES)
        self.assertIn('.agents/plugins/marketplace.json', files)
        self.assertIn('.github/workflows/ci.yml', files)
        self.assertIn('ui/dist/panel.html', files)
        self.assertFalse(any(p.endswith(('.jsonl', '.pyc')) or '__pycache__' in p for p in files))


if __name__ == '__main__':
    unittest.main()
