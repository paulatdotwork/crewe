"""local_workspace.py — UNSANDBOXED workspace on the router's own host.

The no-Docker option: same interface as Sandbox/SSHWorkspace so the agent
loop is backend-agnostic, but every command runs directly as the router's
user, with the router's permissions — no container, no egress allowlist, no
resource limits. That is the deal, not a bug, so it is double-gated: the
operator must set CREWE_AGENT_ALLOW_LOCAL=1 before the target even appears,
and the UI labels it unsandboxed.

write/read/list are contained to the per-run workdir as basic hygiene (a
model path like ../../.bashrc must not silently escape), but `run` is a real
shell — containment of commands is exactly what this backend does NOT offer.
"""
import os
import shutil
import subprocess

EXCLUDES = {"node_modules", ".git", "__pycache__", "venv", ".venv"}


def _clip(s, limit=20_000):
    s = s or ""
    return s if len(s) <= limit else s[:limit] + f"\n…[+{len(s)-limit} chars]"


class LocalWorkspace:
    def __init__(self, run_id, workdir_base=None, workdir=None):
        if workdir:
            # An EXISTING folder the operator picked: the agent works in it
            # directly, editing the real files there, instead of starting in a
            # fresh per-run directory. _abs() still confines every path to this
            # root, so the agent cannot wander out of the folder it was given.
            self.workdir = os.path.abspath(os.path.expanduser(workdir))
        else:
            base = os.path.expanduser(
                workdir_base
                or os.environ.get("CREWE_AGENT_LOCAL_DIR", "~/crewe-agent"))
            self.workdir = os.path.join(base, run_id)

    # -- lifecycle -------------------------------------------------------------
    def __enter__(self):
        os.makedirs(self.workdir, exist_ok=True)
        return self

    def __exit__(self, *exc):
        # the workdir stays (like the SSH backend): it lives on the operator's
        # own disk and the snapshot is what the UI serves
        return False

    # -- operations the agent calls -------------------------------------------
    def run(self, cmd, timeout=180):
        try:
            r = subprocess.run(["sh", "-c", cmd], cwd=self.workdir,
                               capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, "", f"command timed out after {timeout}s"
        return r.returncode, _clip(r.stdout), _clip(r.stderr)

    def _abs(self, path):
        p = os.path.normpath(os.path.join(self.workdir, path))
        if not (p == self.workdir or p.startswith(self.workdir + os.sep)):
            raise ValueError(f"path escapes the workspace: {path!r}")
        return p

    def write_file(self, path, content):
        p = self._abs(path)
        os.makedirs(os.path.dirname(p) or self.workdir, exist_ok=True)
        data = content if isinstance(content, str) else content.decode("utf-8", "replace")
        with open(p, "w") as f:
            f.write(data)

    def read_file(self, path, max_bytes=200_000):
        p = self._abs(path)
        # _abs checks the path STRING; a symlink (file or directory) planted
        # in the workspace could still resolve outside it — refuse those
        real, base = os.path.realpath(p), os.path.realpath(self.workdir)
        if os.path.islink(p) or not (real == base or real.startswith(base + os.sep)):
            return None
        try:
            with open(p, "rb") as f:
                return f.read(max_bytes).decode("utf-8", "replace")
        except (FileNotFoundError, IsADirectoryError):
            return None

    def list_files(self, limit=200):
        out = []
        for root, dirs, files in os.walk(self.workdir):
            dirs[:] = [d for d in dirs if d not in EXCLUDES]
            for f in files:
                out.append(os.path.relpath(os.path.join(root, f), self.workdir))
                if len(out) >= limit:
                    return sorted(out)
        return sorted(out)

    def _copytree(self, src, dst):
        for root, dirs, files in os.walk(src):
            dirs[:] = [d for d in dirs if d not in EXCLUDES]
            rel = os.path.relpath(root, src)
            os.makedirs(os.path.join(dst, rel) if rel != "." else dst,
                        exist_ok=True)
            for f in files:
                full = os.path.join(root, f)
                if os.path.islink(full):     # same exfil hygiene as the zip route
                    continue
                shutil.copy2(full, os.path.join(dst, rel, f)
                             if rel != "." else os.path.join(dst, f))

    def snapshot(self, dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
        self._copytree(self.workdir, dest_dir)
        return dest_dir

    def restore(self, src_dir):
        if not os.path.isdir(src_dir) or not os.listdir(src_dir):
            return False
        os.makedirs(self.workdir, exist_ok=True)
        self._copytree(src_dir, self.workdir)
        return bool(os.listdir(self.workdir))
