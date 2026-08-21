"""sandbox.py — ephemeral Docker workspace for the Crewe code agent (Phase A).

A Sandbox is one disposable container that persists across many agent steps
(so files written in step 1 are still there in step 5), then is torn down. The
agent drives it through a tiny surface: run() / write_file() / read_file().

Isolation (Phase A defaults):
  --network none        no network at all (Phase B swaps in an allowlisted proxy)
  --memory / --cpus     resource caps so a build can't take the box down
  --pids-limit          fork-bomb guard
  --cap-drop ALL        drop all Linux capabilities
  --security-opt no-new-privileges
  no host bind mounts   the container cannot see the host filesystem
Everything is ephemeral: a crash or timeout still tears the container down via
the context manager. This module does NOT import router.py and touches nothing
in the running app.
"""
import os
import shlex
import subprocess
import tempfile
import uuid

DEFAULT_IMAGE = os.environ.get("CREWE_SANDBOX_IMAGE", "crewe-sandbox:latest")
NAME_PREFIX = "crewe-sbx-"
EGRESS_NETWORK = "crewe-egress"                 # --internal net (see setup.sh)
PROXY_URL = "http://crewe-proxy:8888"           # allowlisted egress proxy


class SandboxError(RuntimeError):
    pass


def _docker(*args, timeout=60, input=None):
    """Run a docker CLI command, returning CompletedProcess. Raises on timeout."""
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True,
                              timeout=timeout, input=input)
    except subprocess.TimeoutExpired as e:
        raise SandboxError(f"docker command timed out: {' '.join(args[:3])}…") from e
    except FileNotFoundError as e:
        raise SandboxError("docker CLI not found") from e


# Container user. Non-root by default: uid 0 in-container is the precondition
# for most runtime-escape CVEs and nothing the agent does needs it — pip/npm
# install fine per-user/per-workspace. Set CREWE_SANDBOX_USER=0:0 if a task
# genuinely needs apt-get inside the sandbox (e.g. installing a JDK).
SANDBOX_USER = os.environ.get("CREWE_SANDBOX_USER", "1000:1000")


class Sandbox:
    def __init__(self, image=DEFAULT_IMAGE, *, network="none", memory="1g",
                 cpus="2", pids=256, workdir="/workspace"):
        self.image = image
        self.network = network            # "none" (A) or a docker network name (B)
        self.memory = memory
        self.cpus = str(cpus)
        self.pids = str(pids)
        self.workdir = workdir
        self.cid = None
        self.name = NAME_PREFIX + uuid.uuid4().hex[:12]

    # -- lifecycle ------------------------------------------------------------
    def start(self):
        if self.cid:
            return self
        args = ["run", "-d", "--name", self.name]
        env = []
        if self.network == "egress":
            # sandbox joins the internal (no-internet) net; its only way out is
            # the proxy, which the package managers use via these env vars.
            args += ["--network", EGRESS_NETWORK]
            for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                env += ["-e", f"{k}={PROXY_URL}"]
            env += ["-e", "NO_PROXY=localhost,127.0.0.1"]
        else:
            args += ["--network", "none"]
        args += [
            "--memory", self.memory, "--memory-swap", self.memory,   # =memory → no swap
            "--cpus", self.cpus,
            "--pids-limit", self.pids,
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--workdir", self.workdir,
            *(["--user", SANDBOX_USER] if SANDBOX_USER else []),
            *env,
            self.image,
            # keep it alive so we can exec many steps into one filesystem
            "sleep", "infinity",
        ]
        r = _docker(*args, timeout=120)
        if r.returncode != 0:
            raise SandboxError(f"failed to start sandbox: {r.stderr.strip()}")
        self.cid = r.stdout.strip()
        # ensure the workdir exists (some images lack it). tmpfs /workspace
        # keeps this working under --user even on images whose /workspace is
        # root-owned — but ours chowns it at build (Dockerfile.sandbox).
        self.run(f"mkdir -p {shlex.quote(self.workdir)}", timeout=15)
        return self

    def cleanup(self):
        if self.cid:
            _docker("rm", "-f", self.cid, timeout=30)
            self.cid = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.cleanup()
        return False

    # -- operations the agent calls ------------------------------------------
    def run(self, cmd, timeout=120):
        """Run a shell command inside the sandbox. Returns (exit_code, stdout, stderr).
        Output is truncated so a runaway `cat huge` can't blow up the agent's context.
        A TIMEOUT is an observation (exit 124, like the SSH backend and GNU
        timeout), never an exception — one slow `npm install` at step 9 must
        not unwind the loop and destroy the files from steps 1-8."""
        if not self.cid:
            raise SandboxError("sandbox not started")
        # -w pins cwd to the workspace (docker exec doesn't reliably inherit the
        # container's --workdir); sh -c (not -lc) so a login shell can't cd away.
        try:
            r = _docker("exec", "-w", self.workdir, self.cid, "sh", "-c", cmd,
                        timeout=timeout)
        except SandboxError:
            return 124, "", f"command timed out after {timeout}s"
        return r.returncode, _clip(r.stdout), _clip(r.stderr)

    def write_file(self, path, content):
        """Write a file inside the sandbox. Streamed via the exec user's stdin so
        the file is OWNED by the user that will run it — docker cp preserves the
        host tempfile's uid/mode (600, host uid) and then the in-container user
        can't read its own source files."""
        if not self.cid:
            raise SandboxError("sandbox not started")
        path = self._abs(path)
        self.run(f"mkdir -p {shlex.quote(os.path.dirname(path) or '/')}", timeout=15)
        data = content if isinstance(content, str) else content.decode("utf-8", "replace")
        # path passed as $1 (argv), never interpolated into the shell; content on stdin
        r = _docker("exec", "-i", self.cid, "sh", "-c", 'cat > "$1"', "sh", path,
                    input=data, timeout=60)
        if r.returncode != 0:
            raise SandboxError(f"write_file failed: {r.stderr.strip()}")

    def read_file(self, path, max_bytes=200_000):
        """Read a file from the sandbox as text. Returns None if it doesn't exist.
        Uses docker cp (not `cat | head`, whose pipe would mask cat's exit code)."""
        if not self.cid:
            raise SandboxError("sandbox not started")
        path = self._abs(path)
        code, _, _ = self.run(f"test -f {shlex.quote(path)}", timeout=15)
        if code != 0:
            return None
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tmp = tf.name
        try:
            r = _docker("cp", f"{self.cid}:{path}", tmp, timeout=60)
            if r.returncode != 0:
                return None
            # docker cp copies a symlink AS a link, and open() would then
            # follow it on the HOST — a link planted in the sandbox (by a
            # cloned repo, or the model) must never read a host file
            if os.path.islink(tmp):
                return None
            with open(tmp, "rb") as fh:
                data = fh.read(max_bytes)
            return data.decode("utf-8", "replace")
        finally:
            os.unlink(tmp)

    def list_files(self, limit=200):
        code, out, _ = self.run(
            "find . -type f -not -path './node_modules/*' -not -path './.git/*' "
            f"| head -{limit}")
        return [l[2:] if l.startswith("./") else l
                for l in out.splitlines() if l.strip()]

    def snapshot(self, dest_dir):
        """Copy the workspace to a host dir before teardown (tar with excludes,
        docker cp the tarball out, extract). Part of the workspace interface
        shared with SSHWorkspace so the agent loop is backend-agnostic."""
        os.makedirs(dest_dir, exist_ok=True)
        code, _, err = self.run(
            "tar czf /tmp/ws.tgz --exclude=node_modules --exclude=.git "
            "--exclude=__pycache__ --exclude=venv --exclude=.venv "
            f"-C {shlex.quote(self.workdir)} .", timeout=120)
        if code != 0:
            raise SandboxError(f"snapshot tar failed: {err.strip()[:200]}")
        tgz = os.path.join(dest_dir, "_ws.tgz")
        r = _docker("cp", f"{self.cid}:/tmp/ws.tgz", tgz, timeout=60)
        if r.returncode != 0 or not os.path.exists(tgz):
            raise SandboxError("snapshot copy-out failed"
                               + (f": {r.stderr.strip()[:200]}" if r.stderr else ""))
        try:
            subprocess.run(["tar", "xzf", tgz, "-C", dest_dir],
                           capture_output=True, timeout=60, check=True)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            raise SandboxError(f"snapshot extract failed: {e}")
        finally:
            os.remove(tgz)
        return dest_dir

    def restore(self, src_dir):
        """Seed the workspace from a prior snapshot (inverse of snapshot()).
        Tar on the host, docker cp the tarball in, extract AS THE SANDBOX USER
        — writing via cp alone leaves root-owned files the agent then cannot
        edit, the same uid trap write_file works around."""
        if not os.path.isdir(src_dir) or not os.listdir(src_dir):
            return False
        tgz = tempfile.mktemp(suffix=".tgz")
        try:
            subprocess.run(["tar", "czf", tgz, "-C", src_dir, "."],
                           capture_output=True, timeout=120, check=True)
            _docker("cp", tgz, f"{self.cid}:/tmp/seed.tgz", timeout=60)
            # -m + --no-same-owner: extracting a host-built tarball as the
            # sandbox user otherwise EXITS 2 on ownership warnings while
            # extracting everything — judging by exit code reported failure
            # for a restore that had fully worked. Judge by outcome instead.
            self.run(f"tar xzf /tmp/seed.tgz -C {shlex.quote(self.workdir)} "
                     "-m --no-same-owner 2>/dev/null; rm -f /tmp/seed.tgz",
                     timeout=120)
            _c, out, _e = self.run(f"ls -A {shlex.quote(self.workdir)} | head -3")
            return bool(out.strip())
        except Exception:
            return False
        finally:
            try:
                os.remove(tgz)
            except OSError:
                pass

    def _abs(self, path):
        return path if path.startswith("/") else f"{self.workdir}/{path}"


def _clip(s, limit=20_000):
    if s and len(s) > limit:
        return s[:limit] + f"\n…[truncated {len(s) - limit} chars]"
    return s


# --- standalone self-test (no model, no router.py) --------------------------
if __name__ == "__main__":
    ok, fail = [], []
    def check(name, cond):
        (ok if cond else fail).append(name)
        print(("  PASS  " if cond else "  FAIL  ") + name)

    print(f"image: {DEFAULT_IMAGE}")
    with Sandbox() as sb:
        check("container started", bool(sb.cid))

        sb.write_file("hello.py", "print('hi from sandbox')\n")
        code, out, err = sb.run("python3 hello.py")
        check("runs code it was given", code == 0 and "hi from sandbox" in out)

        check("read_file round-trips", sb.read_file("hello.py").startswith("print("))
        check("read_file missing → None", sb.read_file("nope.txt") is None)

        code, _, _ = sb.run("exit 7")
        check("propagates exit codes", code == 7)

        # network must be OFF in Phase A
        code, _, _ = sb.run("python3 -c \"import urllib.request as u; u.urlopen('http://example.com', timeout=5)\"", timeout=20)
        check("network is blocked (offline sandbox)", code != 0)

        # files persist across steps in the same sandbox
        sb.run("echo step1 > state.txt")
        _, out, _ = sb.run("cat state.txt")
        check("filesystem persists across steps", "step1" in out)

        name = sb.name
    # after context exit, the container is gone
    gone = _docker("ps", "-a", "--filter", f"name={name}", "--format", "{{.Names}}")
    check("container torn down on exit", name not in (gone.stdout or ""))

    print(f"\n{len(ok)} passed, {len(fail)} failed")
    raise SystemExit(1 if fail else 0)
