"""Exercise the installed proxy and listener ownership without any OAuth account."""

from contextlib import redirect_stdout
from io import StringIO
import os
from pathlib import Path
import subprocess
import sys


root = Path(sys.argv[1]).absolute()
os.environ["CLAUDE_CODEX_BRIDGE_HOME"] = str(root)
sys.path.insert(0, str(root))

import bridge_models  # noqa: E402
import launcher  # noqa: E402


bridge_models.verify_install()
output = StringIO()
with redirect_stdout(output):
    bridge_models.make_session()
port_text, directory_text, config_text, key_text, log_text = output.getvalue().strip().split("\t")
directory, config, key, log = map(Path, (directory_text, config_text, key_text, log_text))
process = None
try:
    with log.open("wb") as log_handle:
        process = subprocess.Popen([str(launcher.PROXY), "-config", str(config)], stdout=log_handle, stderr=subprocess.STDOUT)
        launcher.wait_for_private_listener(process, int(port_text))
    print("Proxy listener ownership: PASS")
finally:
    launcher.stop_process(process)
    for path in (config, key, log):
        path.unlink(missing_ok=True)
    directory.rmdir()
