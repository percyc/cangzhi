import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.parametrize("tag,dirty,override,expected", [
    ("v1.3.2", "", "", "v1.3.2"),
    ("", "", "", "v1.3.2+aaaaaaa"),
    ("", " M file", "", "v1.3.2+aaaaaaa.dirty"),
    ("", "", "v1.3.2", "v1.3.2"),
])
def test_build_wrapper_metadata_is_explicit_and_dirty_is_not_a_release(tmp_path, tag, dirty, override, expected):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    source = Path(__file__).resolve().parents[3] / "scripts/with-build-info.sh"
    shutil.copy(source, scripts / "with-build-info.sh")
    (tmp_path / "VERSION").write_text("v1.3.2\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text('#!/bin/sh\ncase "$3" in\nrev-parse) printf "%s\\n" "$TEST_HEAD";;\nstatus) printf "%s" "$TEST_DIRTY";;\ndescribe) printf "%s" "$TEST_TAG";;\nesac\n')
    fake_git.chmod(0o755)
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}", "TEST_HEAD": "a" * 40,
        "TEST_TAG": tag, "TEST_DIRTY": dirty, "CANGZHI_VERSION": override}
    output = subprocess.check_output(["bash", str(scripts / "with-build-info.sh"), sys.executable, "-c",
        'import os,json;print(json.dumps({k:os.environ[k] for k in ("CANGZHI_VERSION","CANGZHI_REVISION","CANGZHI_BUILD_TIME","CANGZHI_BUILD_DIRTY")}))'], env=env, text=True)
    body = json.loads(output)
    assert body["CANGZHI_VERSION"] == expected
    assert body["CANGZHI_REVISION"] == "a" * 40
    assert body["CANGZHI_BUILD_DIRTY"] == ("true" if dirty else "false")
    assert body["CANGZHI_BUILD_TIME"].endswith("Z")
