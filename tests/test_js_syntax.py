"""Optional front-end syntax check: ``node --check views/console/*.js``.

The front-end is a build-less classic-script AstrBot Plugin Page; ``node --check``
catches syntax errors without any npm dependency.  The whole test is skipped when
the ``node`` binary is not available on the machine.
"""

import glob
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONSOLE_DIR = os.path.join(PROJECT_ROOT, "views", "console")

NODE = shutil.which("node")


class TestFrontendSyntax(unittest.TestCase):
    @unittest.skipUnless(NODE, "node is not installed; skipping JS syntax check")
    def test_console_js_files_pass_node_check(self):
        js_files = sorted(glob.glob(os.path.join(CONSOLE_DIR, "*.js")))
        assert js_files, "no JavaScript files found under views/console"
        failures = []
        for path in js_files:
            result = subprocess.run(
                [NODE, "--check", path],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                failures.append(
                    "%s\n%s" % (os.path.basename(path), (result.stderr or "").strip())
                )
        assert failures == [], "\n\n".join(failures)

    def test_index_html_scripts_exist(self):
        index_path = os.path.join(CONSOLE_DIR, "index.html")
        with open(index_path, "r", encoding="utf-8") as handle:
            html = handle.read()
        expected = [
            "icons.js",
            "charts.js",
            "app.js",
            "page-overview.js",
            "page-schedule.js",
            "page-character.js",
            "page-relations.js",
            "page-memory.js",
            "page-surfing.js",
            "page-models.js",
            "page-sessions.js",
        ]
        for script in expected:
            assert script in html, script
            assert os.path.isfile(os.path.join(CONSOLE_DIR, script)), script
        # load order: shared shell files before page modules
        assert html.index("app.js") < html.index("page-overview.js")


if __name__ == "__main__":
    unittest.main()
