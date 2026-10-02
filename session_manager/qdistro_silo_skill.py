"""Install the image-built agent skill into one silo uid's home."""

import os
import pwd
import sys
import uuid
from pathlib import Path

SOURCE = Path("/usr/share/qdistro/agents/skills/silo/SKILL.md")


def install_skill(home: Path, uid: int, source: Path = SOURCE) -> None:
    if uid <= 1000:
        raise ValueError("silo skill requires a non-admin uid")
    if home.is_symlink() or not home.is_dir() or home.stat().st_uid != uid:
        raise ValueError("silo home must be a real directory owned by its uid")

    parent = home
    for name in (".agents", "skills", "qdistro-silo"):
        child = parent / name
        if child.is_symlink():
            raise ValueError(f"refusing symlink in skill path: {child}")
        if child.exists():
            if not child.is_dir() or child.stat().st_uid != uid:
                raise ValueError(f"unsafe skill directory: {child}")
        else:
            child.mkdir(mode=0o700)
        child.chmod(0o700)
        parent = child

    target = parent / "SKILL.md"
    if target.is_symlink() or (target.exists() and
                              (not target.is_file() or target.stat().st_uid != uid)):
        raise ValueError(f"unsafe skill file: {target}")
    staged = parent / f".SKILL.md.{uuid.uuid4().hex}"
    try:
        fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(source.read_bytes())
            os.chmod(staged, 0o644)
            os.replace(staged, target)
        except BaseException:
            if os.path.exists(staged):
                staged.unlink()
            raise
    finally:
        if staged.exists():
            staged.unlink()


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("silo skill installer requires root")
    if len(sys.argv) != 2:
        raise SystemExit("usage: qdistro_silo_skill.py <silo-user>")
    user = pwd.getpwnam(sys.argv[1])
    if user.pw_dir != f"/home/{user.pw_name}":
        raise SystemExit("silo home must be under /home/<name>")
    # All home mutations run with the silo's own authority. A path replacement
    # race can then only affect files this uid could already write.
    SOURCE.read_bytes()
    os.setgroups([])
    os.setgid(user.pw_gid)
    os.setuid(user.pw_uid)
    install_skill(Path(user.pw_dir), user.pw_uid)


if __name__ == "__main__":
    main()
