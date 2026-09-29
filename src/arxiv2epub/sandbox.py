"""Run the untrusted part of a conversion inside a locked-down Docker container.

Everything after the download (unpacking, pdflatex, LaTeXML, Ghostscript,
PyMuPDF and Pillow) runs on files written by the paper's authors. Those
tools can read any file they are pointed at (``\\input{/Users/you/.ssh/id_rsa}``
works in TeX even with ``openin_any=p``), and they can be made to use unbounded
memory, disk or processes.

In the container the paper sees only its own source bundle. There is no network,
the root filesystem is read-only, it runs as an unprivileged user with no
capabilities, and memory, CPU, processes and scratch disk are capped. No host
path is writable: the finished EPUB comes back over stdout, and the host reads
at most ``MAX_OUTPUT_BYTES`` of it.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import os
import shutil
import subprocess
import threading
from pathlib import Path

PKG_DIR = Path(__file__).parent
DOCKERFILE = PKG_DIR / "assets" / "Dockerfile"
IMAGE_REPO = "arxiv2epub"

DEFAULT_MEMORY = "4g"
DEFAULT_CPUS = 2
PIDS_LIMIT = 512
WORK_TMPFS_SIZE = "4g"
MAX_OUTPUT_BYTES = 512 << 20  # the finished EPUB (or LaTeXML log) comes back over stdout


class SandboxError(Exception):
    pass


def _docker() -> str:
    exe = shutil.which("docker")
    if exe is None:
        raise SandboxError(
            "Docker is needed to run conversions in a sandbox. Install Docker Desktop "
            "(brew install --cask docker), or pass --no-sandbox to run without it."
        )
    return exe


def check_available() -> None:
    """Raise :class:`SandboxError` unless the Docker daemon is reachable."""
    docker = _docker()
    try:
        r = subprocess.run([docker, "info", "--format", "{{.ServerVersion}}"],
                           capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        r = None
    if r is None or r.returncode != 0:
        raise SandboxError(
            "Docker is installed but not running. Start Docker Desktop, "
            "or pass --no-sandbox to run without the sandbox."
        )


def _dependencies() -> list[str]:
    try:
        reqs = importlib.metadata.requires("arxiv2epub") or []
    except importlib.metadata.PackageNotFoundError:
        reqs = []
    return sorted(r for r in reqs if "extra ==" not in r)


def image_tag() -> str:
    """Image name tagged with a hash of the package, so code changes trigger a rebuild."""
    h = hashlib.sha256()
    for p in sorted(PKG_DIR.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts:
            h.update(p.relative_to(PKG_DIR).as_posix().encode())
            h.update(p.read_bytes())
    h.update("\n".join(_dependencies()).encode())
    return f"{IMAGE_REPO}:{h.hexdigest()[:12]}"


def ensure_image(rebuild: bool = False) -> str:
    """Build the sandbox image if needed and return its tag."""
    docker = _docker()
    tag = image_tag()
    exists = subprocess.run([docker, "image", "inspect", tag], capture_output=True).returncode == 0
    if exists and not rebuild:
        return tag
    print(f"      building sandbox image {tag} (the first build downloads TeX Live, several GB)")
    cmd = [docker, "build", "-t", tag, "-f", str(DOCKERFILE),
           "--build-arg", f"DEPS={' '.join(_dependencies())}", str(PKG_DIR)]
    if subprocess.run(cmd).returncode != 0:
        raise SandboxError("Building the sandbox image failed (see the Docker output above).")
    _remove_old_images(docker, keep=tag)
    return tag


def _remove_old_images(docker: str, keep: str) -> None:
    out = subprocess.run([docker, "image", "ls", IMAGE_REPO, "--format", "{{.Repository}}:{{.Tag}}"],
                         capture_output=True, text=True).stdout.split()
    old = [t for t in out if t != keep]
    if old:
        subprocess.run([docker, "image", "rm", *old], capture_output=True)


def docker_cmd(
    image: str, bundle: Path, meta_json: Path, *, container: str, timeout: int,
    memory: str = DEFAULT_MEMORY, cpus: int = DEFAULT_CPUS,
    work_dir: Path | None = None, verbose: bool = False,
) -> list[str]:
    """The ``docker run`` command line for converting ``bundle``.

    Nothing on the host is writable (unless ``work_dir`` is given for debugging):
    the container prints its result to stdout and progress to stderr.
    """
    scratch = "rw,nosuid,nodev,noexec,mode=1777"
    cmd = [
        _docker(), "run", "--rm", "--name", container,
        "--network=none",
        "--read-only",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        f"--memory={memory}", f"--memory-swap={memory}",  # equal: no swap on top
        f"--cpus={cpus}",
        f"--pids-limit={PIDS_LIMIT}",
        "--tmpfs", f"/tmp:{scratch},size=512m",
        "--mount", f"type=bind,src={bundle.resolve()},dst=/in/source.bin,readonly",
        "--mount", f"type=bind,src={meta_json.resolve()},dst=/in/meta.json,readonly",
    ]
    if work_dir is None:
        cmd += ["--tmpfs", f"/work:{scratch},size={WORK_TMPFS_SIZE}"]
    else:
        cmd += ["--mount", f"type=bind,src={work_dir.resolve()},dst=/work"]
    cmd += [image, "_build", "/in/source.bin", "/in/meta.json",
            "--work", "/work", "--timeout", str(timeout)]
    if verbose:
        cmd.append("--verbose")
    return cmd


def run(bundle: Path, meta_json: Path, dest: Path, *, timeout: int,
        memory: str = DEFAULT_MEMORY, work_dir: Path | None = None,
        log_dest: Path | None = None, rebuild: bool = False, verbose: bool = False) -> None:
    """Convert ``bundle`` to ``dest`` inside the sandbox.

    ``timeout`` is the per-step limit passed to the converter; the whole run is
    killed after a few times that. On failure, LaTeXML's log is saved to ``log_dest``.
    """
    check_available()
    image = ensure_image(rebuild)
    container = f"arxiv2epub-{os.getpid()}"
    if work_dir is not None:
        if work_dir.exists():
            shutil.rmtree(work_dir)
        work_dir.mkdir(parents=True)
    cmd = docker_cmd(image, bundle, meta_json, container=container, timeout=timeout,
                     memory=memory, work_dir=work_dir, verbose=verbose)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(f".{dest.name}.part")
    try:
        code = _run_capturing(cmd, part, container, timeout * 4)
        if code != 0:
            if log_dest is not None and part.stat().st_size:
                log_dest.parent.mkdir(parents=True, exist_ok=True)
                part.replace(log_dest)
                print(f"      full LaTeXML log saved to {log_dest}")
            raise SandboxError(explain_exit(code, memory))
        if not part.stat().st_size:
            raise SandboxError("The sandbox finished but produced no EPUB.")
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)


def _run_capturing(cmd: list[str], dest: Path, container: str, deadline: int) -> int:
    """Run ``cmd``, saving at most ``MAX_OUTPUT_BYTES`` of its stdout to ``dest``.

    Returns the exit code. Kills the container on timeout, oversize output or Ctrl-C.
    """
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
    timed_out = threading.Event()

    def kill() -> None:
        subprocess.run([_docker(), "kill", container], capture_output=True)
        proc.kill()

    def on_timeout() -> None:
        timed_out.set()
        kill()

    timer = threading.Timer(deadline, on_timeout)
    timer.start()
    size = 0
    try:
        with dest.open("wb") as f:
            while chunk := proc.stdout.read(1 << 16):
                size += len(chunk)
                if size > MAX_OUTPUT_BYTES:
                    kill()
                    raise SandboxError(
                        f"The sandbox produced more than {MAX_OUTPUT_BYTES >> 20} MB of output; stopped."
                    )
                f.write(chunk)
        code = proc.wait()
    except BaseException:
        kill()
        proc.wait()
        raise
    finally:
        timer.cancel()
        proc.stdout.close()
    if timed_out.is_set():
        raise SandboxError(f"The conversion took longer than {deadline}s and was stopped.")
    return code


def explain_exit(code: int, memory: str) -> str:
    if code == 137:
        return (f"The conversion was killed, most likely for using more than {memory} of memory. "
                f"Try --sandbox-memory 8g if the paper is unusually large.")
    if code == 130:
        return "Interrupted."
    if code in (125, 126, 127):
        return f"Docker could not start the sandbox (exit {code}); see the Docker error above."
    return "The conversion failed inside the sandbox (see the error above)."
