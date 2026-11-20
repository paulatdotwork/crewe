"""ssh_workspace.py — a remote-host workspace for the Crewe code agent (Phase E).

Same interface as sandbox.Sandbox (start/run/write_file/read_file/list_files/
snapshot/cleanup + context manager), so agent.run_agent drives a remote machine
over SSH exactly as it drives a local container.

Security posture (the router enforces the first two; this class enforces the rest):
  - HOST ALLOWLIST: the router only ever constructs this for a host named in
    ~/crewe_agent_hosts.json. The browser sends a NAME, never a hostname.
  - KEY-ONLY auth: BatchMode=yes — a host needing a password just fails, it
    never prompts or accepts one from the app.
  - Per-run workdir: all work happens under <base>/<run_id>; cleanup() does NOT
    delete it (it's a real machine — the user may want the files).
  - Full audit happens in agent.py (every command is logged) — extra important
    here because these run on a real box, not a disposable container.

Unlike the Docker sandbox this is NOT isolation — the agent can do whatever the
SSH user can. Use a dedicated, non-root, non-sudo user on the remote.
"""
import os
import shlex
import subprocess
import uuid


class WorkspaceError(RuntimeError):
    pass


def _clip(s, limit=20_000):
    if s and len(s) > limit:
        return s[:limit] + f"\n…[truncated {len(s) - limit} chars]"
    return s


class SSHWorkspace:
    def __init__(self, host, user, *, port=22, key=None,
                 workdir_base="~/crewe-agent", run_id=None, workdir=None):
        self.host = host
        self.user = user
        self.port = int(port or 22)
        self.key = key
        self.workdir_base = workdir_base or "~/crewe-agent"
        # An EXISTING remote folder to work in, instead of a fresh per-run dir.
        self.pinned_workdir = workdir
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.workdir = None            # resolved on start()
        self._started = False

    def _ssh(self, extra_timeout=10):
        opts = ["ssh",
                "-o", "BatchMode=yes",                    # never prompt; key-only
                "-o", "StrictHostKeyChecking=accept-new", # TOFU on first connect
                "-o", f"ConnectTimeout={extra_timeout}",
                "-p", str(self.port)]
        if self.key:
            opts += ["-i", os.path.expanduser(self.key)]
        opts.append(f"{self.user}@{self.host}")
        return opts

    def start(self):
        if self._started:
            return self
        # resolve ~ against the remote $HOME (shlex.quote would stop the shell
        # from expanding a literal ~), then make the per-run workdir
        r = subprocess.run(self._ssh() + ["echo $HOME"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise WorkspaceError(f"ssh connect failed for {self.user}@{self.host}: "
                                 f"{r.stderr.strip()}")
        home = r.stdout.strip() or "."
        if self.pinned_workdir:
            wd = self.pinned_workdir
            self.workdir = (home + wd[1:]) if wd.startswith("~") else wd
        else:
            base = self.workdir_base
            if base.startswith("~"):
                base = home + base[1:]
            self.workdir = f"{base.rstrip('/')}/{self.run_id}"
        r = subprocess.run(self._ssh() + [f"mkdir -p {shlex.quote(self.workdir)}"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise WorkspaceError(f"could not create workdir: {r.stderr.strip()}")
        self._started = True
        return self

    def cleanup(self):
        # deliberately does NOT delete the remote workdir — real machine.
        self._started = False

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.cleanup()
        return False

    def run(self, cmd, timeout=180):
        full = f"cd {shlex.quote(self.workdir)} && ({cmd})"
        try:
            r = subprocess.run(self._ssh() + [full], capture_output=True,
                               text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, "", f"command timed out after {timeout}s"
        return r.returncode, _clip(r.stdout), _clip(r.stderr)

    def write_file(self, path, content):
        p = self._abs(path)
        self.run(f"mkdir -p {shlex.quote(os.path.dirname(p) or '.')}", timeout=15)
        data = content if isinstance(content, str) else content.decode("utf-8", "replace")
        r = subprocess.run(self._ssh() + [f"cat > {shlex.quote(p)}"],
                           input=data, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise WorkspaceError(f"write_file failed: {r.stderr.strip()}")

    def read_file(self, path, max_bytes=200_000):
        p = self._abs(path)
        r = subprocess.run(
            # `! -L`: a symlink (planted by a cloned repo, or the model) must
            # never be followed on the remote box by the harness's own reads
            self._ssh() + [f"test -f {shlex.quote(p)} && test ! -L {shlex.quote(p)} "
                           f"&& cat {shlex.quote(p)} || exit 44"],
            capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return None
        return r.stdout[:max_bytes]

    def list_files(self, limit=200):
        code, out, _ = self.run(
            "find . -type f -not -path './node_modules/*' -not -path './.git/*' "
            f"| head -{limit}")
        return [l[2:] if l.startswith("./") else l
                for l in out.splitlines() if l.strip()]

    def snapshot(self, dest_dir):
        """Stream the remote workdir back as a tarball and extract locally."""
        os.makedirs(dest_dir, exist_ok=True)
        tar = (f"cd {shlex.quote(self.workdir)} && tar czf - "
               "--exclude=node_modules --exclude=.git --exclude=__pycache__ "
               "--exclude=venv --exclude=.venv . 2>/dev/null")
        tgz = os.path.join(dest_dir, "_ws.tgz")
        with open(tgz, "wb") as f:
            subprocess.run(self._ssh() + [tar], stdout=f,
                           stderr=subprocess.PIPE, timeout=120)
        if os.path.getsize(tgz) > 0:
            subprocess.run(["tar", "xzf", tgz, "-C", dest_dir],
                           capture_output=True, timeout=60)
        os.remove(tgz)
        return dest_dir

    def _abs(self, path):
        return path if path.startswith("/") else f"{self.workdir}/{path}"
