import subprocess
import sys
import unittest


class ImporterCompatibilityTest(unittest.TestCase):
    def check_import(self, name):
        code = """
from beets import importer

actions = getattr(importer, 'Action', None)
if actions is None:
    actions = importer.action
for name in ('Action', 'action'):
    if hasattr(importer, name):
        delattr(importer, name)
setattr(importer, {name!r}, actions)

from beetsplug import oldestdate

assert oldestdate.action is actions
assert oldestdate.action.SKIP is actions.SKIP
assert oldestdate.ImportTask is importer.ImportTask
assert oldestdate.ImportSession is importer.ImportSession
""".format(name=name)
        result = subprocess.run(
            [sys.executable, '-c', code],
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_modern_action_import(self):
        self.check_import('Action')

    def test_legacy_action_import(self):
        self.check_import('action')
