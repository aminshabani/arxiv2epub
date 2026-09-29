import os
import subprocess
import sys

import pytest

from arxiv2epub import sandbox


def _cmd(tmp_path, **kw):
    bundle, meta = tmp_path / "source.bin", tmp_path / "meta.json"
    bundle.write_bytes(b"x")
    meta.write_text("{}")
    return sandbox.docker_cmd("arxiv2epub:test", bundle, meta, container="c1", timeout=600, **kw)


def test_docker_cmd_is_locked_down(tmp_path):
    cmd = _cmd(tmp_path)
    for flag in ("--network=none", "--read-only", "--cap-drop=ALL",
                 "--security-opt=no-new-privileges", "--memory=4g", "--memory-swap=4g",
                 "--pids-limit=512", "--rm"):
        assert flag in cmd
    assert "--user" in cmd
    assert "--privileged" not in cmd
    mounts = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--mount"]
    assert len(mounts) == 2 and all(m.endswith(",readonly") for m in mounts)  # nothing writable
    assert any("dst=/in/source.bin,readonly" in m for m in mounts)
    tmpfs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--tmpfs"]
    assert any(t.startswith("/work:") and "noexec" in t for t in tmpfs)
    assert cmd[cmd.index("arxiv2epub:test") + 1:][:3] == ["_build", "/in/source.bin", "/in/meta.json"]


def test_docker_cmd_keep_work_mounts_host_dir(tmp_path):
    cmd = _cmd(tmp_path, work_dir=tmp_path / "work", memory="8g")
    assert "--memory=8g" in cmd
    assert f"type=bind,src={(tmp_path / 'work').resolve()},dst=/work" in cmd
    assert not any(a.startswith("/work:") for a in cmd)


def test_explain_exit():
    assert "memory" in sandbox.explain_exit(137, "4g")
    assert "Docker could not start" in sandbox.explain_exit(125, "4g")
    assert "failed inside the sandbox" in sandbox.explain_exit(1, "4g")


def test_check_available_daemon_down(monkeypatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(sandbox.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "no daemon"))
    with pytest.raises(sandbox.SandboxError, match="not running"):
        sandbox.check_available()


def test_check_available_not_installed(monkeypatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: None)
    with pytest.raises(sandbox.SandboxError, match="--no-sandbox"):
        sandbox.check_available()


@pytest.fixture
def fake_container(tmp_path, monkeypatch):
    """Make sandbox.run execute a local Python snippet instead of docker."""
    monkeypatch.setattr(sandbox, "check_available", lambda: None)
    monkeypatch.setattr(sandbox, "ensure_image", lambda rebuild=False: "arxiv2epub:test")
    monkeypatch.setattr(sandbox, "_docker", lambda: "true")
    bundle, meta = tmp_path / "s.bin", tmp_path / "m.json"
    bundle.write_bytes(b"x")
    meta.write_text("{}")

    def run(script, **kw):
        monkeypatch.setattr(sandbox, "docker_cmd", lambda *a, **k: [sys.executable, "-c", script])
        dest = tmp_path / "books" / "p.epub"
        sandbox.run(bundle, meta, dest, timeout=10, **kw)
        return dest

    return run


def test_run_saves_stdout_as_epub(fake_container):
    dest = fake_container("import sys; sys.stdout.buffer.write(b'epub')")
    assert dest.read_bytes() == b"epub"
    assert [p.name for p in dest.parent.iterdir()] == ["p.epub"]


def test_run_failure_saves_log(fake_container, tmp_path):
    log = tmp_path / "latexml.log"
    with pytest.raises(sandbox.SandboxError, match="failed inside"):
        fake_container("import sys; sys.stdout.write('Fatal: x'); sys.exit(1)", log_dest=log)
    assert log.read_text() == "Fatal: x"
    assert not list((tmp_path / "books").iterdir())


def test_run_caps_output(fake_container, tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "MAX_OUTPUT_BYTES", 100_000)
    with pytest.raises(sandbox.SandboxError, match="more than"):
        fake_container("import sys\nwhile True: sys.stdout.buffer.write(b'x' * 65536)")
    assert not list((tmp_path / "books").iterdir())


def test_run_timeout(monkeypatch):
    monkeypatch.setattr(sandbox, "_docker", lambda: "true")
    with pytest.raises(sandbox.SandboxError, match="longer than"):
        sandbox._run_capturing([sys.executable, "-c", "import time; time.sleep(30)"],
                               sandbox.Path(os.devnull), "c1", deadline=1)
