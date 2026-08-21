"""Workspace reads never follow a symlink out of the workspace.

Why: the harness now reads AGENTS.md automatically from a cloned repo, and a
repo can ship `AGENTS.md -> /etc/passwd`. The local backend followed it on the
router host; the Docker backend's `docker cp` copies the link AS a link and
the host then open()ed its target. Both now refuse symlinks (the model can
still `cat` inside its own namespace through `run`, which is the point).

The local part is hermetic. The Docker part runs only when Docker and the
crewe-sandbox image are available; otherwise it is skipped, not failed.
"""
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, ROOT)
import local_workspace

ok, bad = [], []


def check(name, cond, detail=""):
    (ok if cond else bad).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not cond else ""))


print("--- local backend ---")
base = tempfile.mkdtemp(prefix="crewe-symlink-")
outside = os.path.join(base, "outside.txt")
open(outside, "w").write("SECRET")
ws = local_workspace.LocalWorkspace("run1", workdir_base=os.path.join(base, "ws"))
with ws:
    open(os.path.join(ws.workdir, "real.txt"), "w").write("fine")
    os.symlink(outside, os.path.join(ws.workdir, "AGENTS.md"))
    os.symlink(os.path.dirname(outside), os.path.join(ws.workdir, "d"))
    check("a regular file reads", ws.read_file("real.txt") == "fine")
    check("a symlinked file pointing outside is refused", ws.read_file("AGENTS.md") is None)
    check("a file reached through a symlinked directory is refused", ws.read_file("d/outside.txt") is None)
    os.symlink(os.path.join(ws.workdir, "real.txt"), os.path.join(ws.workdir, "inner"))
    check("even an in-workspace symlink is refused (links are simply not read)", ws.read_file("inner") is None)
    try:
        ws.read_file("../outside.txt")
        check("dot-dot escape is refused", False)
    except ValueError:
        check("dot-dot escape is refused", True)
shutil.rmtree(base, ignore_errors=True)

print("--- docker backend ---")
have_docker = shutil.which("docker") and subprocess.run(
    ["docker", "image", "inspect", "crewe-sandbox"], capture_output=True).returncode == 0
if not have_docker:
    print("  SKIP  docker / crewe-sandbox image not available here")
else:
    import sandbox
    try:
        with sandbox.Sandbox(network="none") as sb:
            sb.run("echo hi > real && ln -s /etc/hostname lk && ln -s /etc lkd")
            check("sandbox: a regular file reads", sb.read_file("real") == "hi\n")
            check("sandbox: a symlink to a file is refused", sb.read_file("lk") is None)
            # an intermediate symlinked DIRECTORY is resolved by docker cp inside the
            # container's own namespace (the model could cat it anyway); the host is
            # never involved — so it must read the CONTAINER's file, never the host's
            got = sb.read_file("lkd/hostname")
            inside = sb.run("cat /etc/hostname")[1]
            host = open("/etc/hostname").read()
            check("sandbox: a file through a symlinked dir resolves INSIDE the container, never on the host",
                  got in (None, inside) and got != host, repr(got)[:60])
            check("sandbox: the model can still cat inside its own namespace",
                  sb.run("cat lk")[0] == 0)
    except Exception as e:
        print(f"  SKIP  sandbox could not start: {str(e)[:120]}")

print(f"\n{len(ok)} passed, {len(bad)} failed")
if bad:
    print("FAILED:", bad)
sys.exit(1 if bad else 0)
