"""``qterminator-shell-integration`` CLI — install OSC 133 / 7 / 8 hooks
for the user's shell.

The hook scripts emit the same well-trodden sequences kitty / vscode
use; nothing novel. The installer writes a single ``eval`` /
``source`` line into the user's shell init file (``.bashrc`` /
``.zshrc`` / ``conf.d/qterminator.fish``) and prints the hook script
to a stable path under ``$XDG_DATA_HOME`` so it can be re-edited
without re-running the installer.

The shell-integration plugin in QTerminator parses the sequences this
hook emits — so once a user runs ``qterminator-shell-integration
install bash``, every new shell session reports prompts / commands /
working-directory / exit codes to QTerminator's command-history
service.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Hook scripts emit:
#   OSC 133 ;A          before each prompt (PROMPT precmd)
#   OSC 133 ;B          after prompt, before command runs (between PS1
#                       and user input — handled by readline hook in
#                       bash/zsh, fish event in fish)
#   OSC 133 ;C          when the command starts executing
#   OSC 133 ;D ; <rc>   after the command finishes
#   OSC 7 ;file://h/p   on PWD change
#
# References:
#   - https://gitlab.freedesktop.org/Per_Bothner/specifications/-/blob/master/proposals/semantic-prompts.md
#   - kitty: https://sw.kovidgoyal.net/kitty/shell-integration/
#   - vscode terminal shell integration

BASH_HOOK = r"""# qterminator shell-integration hook for bash
# Emits OSC 133 prompt / command / exit-code marks and OSC 7 CWD updates.
# Source from your .bashrc:  source "${XDG_DATA_HOME:-$HOME/.local/share}/qterminator/shell-integration.bash"

if [[ -n "${QTERMINATOR_SHELL_INTEGRATION_DONE:-}" ]]; then
    return
fi
QTERMINATOR_SHELL_INTEGRATION_DONE=1

__qterm_osc7() {
    local enc
    # printf %q quotes for shell; we want URL encoding instead — bash
    # has no builtin, but spaces and a few others matter most.
    local p="$PWD"
    p="${p// /%20}"
    printf '\033]7;file://%s%s\033\\' "$HOSTNAME" "$p"
}

__qterm_prompt_start() {
    printf '\033]133;A\033\\'
}

__qterm_prompt_end() {
    printf '\033]133;B\033\\'
}

__qterm_preexec() {
    printf '\033]133;C\033\\'
}

__qterm_precmd() {
    local rc=$?
    printf '\033]133;D;%s\033\\' "$rc"
    __qterm_osc7
}

# Wire PROMPT_COMMAND to fire precmd and prompt_start before each prompt.
PROMPT_COMMAND="__qterm_precmd; __qterm_prompt_start${PROMPT_COMMAND:+; $PROMPT_COMMAND}"

# Inject ;B at the very end of PS1 and rely on bash-preexec.sh-style
# DEBUG trap for ;C. bash-preexec is the simplest portable path.
if [[ -z "${__bp_imported:-}" ]] && [[ -f "${XDG_DATA_HOME:-$HOME/.local/share}/qterminator/bash-preexec.sh" ]]; then
    source "${XDG_DATA_HOME:-$HOME/.local/share}/qterminator/bash-preexec.sh"
fi
if type preexec_functions >/dev/null 2>&1; then
    preexec_functions+=(__qterm_preexec)
fi

# Append OSC 133 ;B to PS1 so the start-of-command boundary is visible.
case "$PS1" in
    *\\[\\033\\]133*) ;;  # already present
    *) PS1="$PS1\[\033]133;B\033\\\\\]" ;;
esac
"""


ZSH_HOOK = r"""# qterminator shell-integration hook for zsh
# Source from .zshrc:  source "${XDG_DATA_HOME:-$HOME/.local/share}/qterminator/shell-integration.zsh"

if [[ -n "${QTERMINATOR_SHELL_INTEGRATION_DONE:-}" ]]; then
    return
fi
typeset -g QTERMINATOR_SHELL_INTEGRATION_DONE=1

__qterm_osc7() {
    local p="${PWD// /%20}"
    printf '\033]7;file://%s%s\033\\' "$HOST" "$p"
}

__qterm_precmd() {
    local rc=$?
    printf '\033]133;D;%s\033\\' "$rc"
    __qterm_osc7
    printf '\033]133;A\033\\'
}

__qterm_preexec() {
    printf '\033]133;C\033\\'
}

autoload -U add-zsh-hook
add-zsh-hook precmd  __qterm_precmd
add-zsh-hook preexec __qterm_preexec

# Append OSC 133 ;B at the end of PS1 so command-start is at end-of-prompt.
case "$PS1" in
    *$'\033]133;B'*) ;;
    *) PS1="$PS1"$'\033]133;B\033\\' ;;
esac
"""


FISH_HOOK = r"""# qterminator shell-integration hook for fish
# Drop into ~/.config/fish/conf.d/qterminator.fish

if set -q QTERMINATOR_SHELL_INTEGRATION_DONE
    exit 0
end
set -g QTERMINATOR_SHELL_INTEGRATION_DONE 1

function __qterm_osc7 --on-variable PWD
    set -l p (string replace -a ' ' '%20' -- $PWD)
    printf '\033]7;file://%s%s\033\\' (hostname) $p
end

function __qterm_precmd --on-event fish_prompt
    printf '\033]133;A\033\\'
end

function __qterm_postexec --on-event fish_postexec
    printf '\033]133;D;%s\033\\' $status
    __qterm_osc7
end

function __qterm_preexec --on-event fish_preexec
    printf '\033]133;C\033\\'
end

# Wrap fish_prompt to append OSC 133 ;B.
functions -c fish_prompt __qterm_orig_fish_prompt 2>/dev/null
function fish_prompt
    __qterm_orig_fish_prompt
    printf '\033]133;B\033\\'
end
"""


HOOKS = {
    "bash": ("shell-integration.bash", BASH_HOOK,
             "~/.bashrc",
             'source "${XDG_DATA_HOME:-$HOME/.local/share}/qterminator/shell-integration.bash"'),
    "zsh":  ("shell-integration.zsh", ZSH_HOOK,
             "~/.zshrc",
             'source "${XDG_DATA_HOME:-$HOME/.local/share}/qterminator/shell-integration.zsh"'),
    "fish": ("shell-integration.fish", FISH_HOOK,
             "~/.config/fish/conf.d/qterminator.fish",
             None),  # fish ships the hook directly into conf.d
}


def _data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "qterminator"


def install(shell: str, dry_run: bool = False) -> int:
    if shell not in HOOKS:
        print(f"unknown shell: {shell!r}; want one of bash/zsh/fish",
              file=sys.stderr)
        return 2
    filename, hook, rc_path, source_line = HOOKS[shell]
    data_dir = _data_dir()
    hook_path = data_dir / filename

    if dry_run:
        print(f"would write hook to: {hook_path}")
        if shell == "fish":
            fish_dir = Path(os.path.expanduser("~/.config/fish/conf.d"))
            print(f"would symlink:       {fish_dir / 'qterminator.fish'} -> {hook_path}")
        else:
            print(f"would append to:     {rc_path}")
            print(f"  {source_line}")
        return 0

    data_dir.mkdir(parents=True, exist_ok=True)
    hook_path.write_text(hook)
    print(f"wrote: {hook_path}")

    if shell == "fish":
        fish_dir = Path(os.path.expanduser("~/.config/fish/conf.d"))
        fish_dir.mkdir(parents=True, exist_ok=True)
        link = fish_dir / "qterminator.fish"
        if link.is_symlink() or link.exists():
            link.unlink()
        try:
            link.symlink_to(hook_path)
            print(f"linked: {link} -> {hook_path}")
        except OSError as e:
            # Cross-fs / no-symlink filesystems: copy instead.
            link.write_text(hook)
            print(f"copied (symlink failed: {e}): {link}")
        return 0

    rc_real = Path(os.path.expanduser(rc_path))
    existing = rc_real.read_text() if rc_real.exists() else ""
    if source_line in existing:
        print(f"already sourced in {rc_real}")
        return 0
    rc_real.parent.mkdir(parents=True, exist_ok=True)
    with rc_real.open("a") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write("\n# qterminator shell integration\n")
        f.write(source_line + "\n")
    print(f"appended source line to {rc_real}")
    return 0


def print_hook(shell: str) -> int:
    if shell not in HOOKS:
        print(f"unknown shell: {shell!r}", file=sys.stderr)
        return 2
    sys.stdout.write(HOOKS[shell][1])
    return 0


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="qterminator-shell-integration",
        description="Install QTerminator's OSC 133/7/8 shell hooks.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    inst = sub.add_parser("install", help="Install shell hooks.")
    inst.add_argument("shell", choices=sorted(HOOKS.keys()))
    inst.add_argument("--dry-run", action="store_true",
                      help="Print what would be done; don't touch the filesystem.")
    show = sub.add_parser("print", help="Print the hook script to stdout.")
    show.add_argument("shell", choices=sorted(HOOKS.keys()))
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.cmd == "install":
        return install(args.shell, dry_run=args.dry_run)
    if args.cmd == "print":
        return print_hook(args.shell)
    return 1


if __name__ == "__main__":
    sys.exit(main())
