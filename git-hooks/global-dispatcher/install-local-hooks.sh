#!/bin/sh
# install-local-hooks.sh — wire ONE repo into the global git-hook dispatcher.
#
# THE PROBLEM IT SOLVES
#   A global `core.hooksPath = ~/.config/git/hooks` makes every global hook fire in
#   normal repos. But a repo with a LOCAL hook manager overrides core.hooksPath
#   (lefthook -> .git/hooks, husky -> .husky/_) or ships raw .git/hooks, and those
#   SHADOW the global hooks entirely. This script injects ONE call to the dispatcher
#   (`run-global-hooks <event>`) into whatever the repo already uses, so EVERY global
#   hook in ~/.config/git/global-hooks.d/<event>/ runs there too — now and forever,
#   with no further per-repo edits when new global hooks are added.
#
# WHY ONE LINE COVERS ALL FUTURE HOOKS
#   We never inject individual hooks (secret-scan etc.). We inject the dispatcher,
#   which enumerates global-hooks.d/<event>/ at runtime. Drop a new file there and it
#   runs in every wired repo automatically.
#
# IDEMPOTENT
#   Every injection is guarded by a marker comment (MARKER below). Re-running is a
#   no-op once present. Nothing is ever clobbered: lefthook gets a MERGED command,
#   husky/raw get an APPENDED guarded line, and an existing raw hook keeps its body.
#
# USAGE
#   install-local-hooks.sh [REPO_DIR]      # default: the repo containing $PWD
#   Detects the manager automatically: lefthook (lefthook.yml) | husky (.husky/) |
#   raw (.git/hooks). For lefthook in a pull-only checkout, edit lefthook.yml on a
#   branch and commit it — this script edits the file in place; commit it yourself.
#
# EXIT: 0 on success / already-wired; non-zero on a real failure.

set -eu

MARKER="global-git-hooks-dispatcher"          # idempotency marker (in injected text)
DISPATCHER='"${XDG_CONFIG_HOME:-$HOME/.config}/git/run-global-hooks"'
EVENTS="pre-commit commit-msg pre-push"

REPO="${1:-$(git rev-parse --show-toplevel 2>/dev/null || true)}"
[ -n "$REPO" ] || { echo "install-local-hooks: not in a git repo and no REPO_DIR given" >&2; exit 2; }
[ -e "$REPO/.git" ] || { echo "install-local-hooks: $REPO is not a git repo" >&2; exit 2; }
REPO="$(cd "$REPO" && pwd)"

log() { echo "install-local-hooks[$(basename "$REPO")]: $*"; }

# --- manager detection ------------------------------------------------------------
# Order matters: a repo can have BOTH a lefthook.yml and .husky; prefer whichever
# actually owns the git hooks. We detect by config file presence, lefthook first.
detect_manager() {
  if [ -f "$REPO/lefthook.yml" ] || [ -f "$REPO/lefthook.yaml" ] || [ -f "$REPO/.lefthook.yml" ]; then
    echo lefthook; return
  fi
  if [ -d "$REPO/.husky" ]; then echo husky; return; fi
  echo raw
}

# ----------------------------------------------------------------------------------
# lefthook: add (or merge) a `<event>.commands.global-git-hooks` entry that runs the
# dispatcher. We do NOT clobber the file — we only add commands that are missing,
# keyed by the marker. lefthook passes the commit-msg file path as {1}.
# ----------------------------------------------------------------------------------
inject_lefthook() {
  cfg="$REPO/lefthook.yml"; [ -f "$cfg" ] || cfg="$REPO/lefthook.yaml"; [ -f "$cfg" ] || cfg="$REPO/.lefthook.yml"
  if grep -q "$MARKER" "$cfg" 2>/dev/null; then
    log "lefthook already wired ($cfg) — no-op"; return 0
  fi
  log "wiring lefthook ($cfg)"
  # Append a self-contained block. lefthook merges multiple top-level <event> keys?
  # No — a YAML doc can't have duplicate keys. So we APPEND new command entries under
  # each event IF the event key exists, else create the event with our command. We do
  # this with a tiny awk that is YAML-indent aware for the common 2-space lefthook style.
  tmp="$(mktemp "${TMPDIR:-/tmp}/lefthook.XXXXXX")"
  # The dispatcher command for each event. The WHOLE shell command must be a single
  # YAML scalar, so we single-quote it: `run: '"$DISP" pre-commit'`. Inside a YAML
  # single-quoted scalar the `${...}` default and the embedded double-quotes are
  # literal — lefthook hands the string to `sh -c`, which then expands them. (An
  # unquoted `run: "..." pre-commit` is INVALID YAML — quoted scalar + trailing junk.)
  #
  # We pass every awk var as a SINGLE LINE — BSD/macOS awk rejects a literal newline
  # inside a `-v` value ("awk: newline in string"). awk emits the key line and the
  # run line as two separate prints.
  rgh='"${XDG_CONFIG_HOME:-$HOME/.config}/git/run-global-hooks"'
  keyline="    $MARKER:"
  awk -v keyline="$keyline" \
      -v run_pc="      run: '$rgh pre-commit'" \
      -v run_cm="      run: '$rgh commit-msg {1}'" \
      -v run_pp="      run: '$rgh pre-push'" '
    function emit(ev) {
      print keyline
      if (ev == "pre-commit") print run_pc
      else if (ev == "commit-msg") print run_cm
      else if (ev == "pre-push") print run_pp
    }
    BEGIN {
      split("pre-commit commit-msg pre-push", order, " ")
      for (k = 1; k <= 3; k++) { e = order[k]; hdr[e] = 0; cmds[e] = 0 }
    }
    # Record, per event: the header line index (hdr) and its `commands:` line index
    # (cmds, 0 if the block has no commands: key — e.g. a scripts:-only block).
    {
      lines[NR] = $0
      if ($0 ~ /^(pre-commit|commit-msg|pre-push):[ \t]*$/) {
        ev = $0; sub(/:.*/, "", ev); cur_event = ev; hdr[ev] = NR
      } else if ($0 ~ /^[^ \t#]/) {
        cur_event = ""   # left the block (some other top-level key)
      }
      if (cur_event != "" && cmds[cur_event] == 0 && $0 ~ /^[ \t]+commands:[ \t]*$/) {
        cmds[cur_event] = NR
      }
    }
    END {
      # Decide the injection point for each present event.
      #  - has commands:  -> inject right AFTER the commands: line.
      #  - block but no commands: -> inject right AFTER the header, prefixing a commands: line.
      for (k = 1; k <= 3; k++) {
        e = order[k]
        if (cmds[e] > 0)      { after_cmds[cmds[e]] = e }
        else if (hdr[e] > 0)  { after_hdr[hdr[e]]  = e }
      }
      for (i = 1; i <= NR; i++) {
        print lines[i]
        if (i in after_cmds) emit(after_cmds[i])
        if (i in after_hdr)  { print "  commands:"; emit(after_hdr[i]) }
      }
      # Events with no block at all -> append a fresh one.
      for (k = 1; k <= 3; k++) {
        e = order[k]
        if (hdr[e] == 0) { print e ":"; print "  commands:"; emit(e) }
      }
    }
  ' "$cfg" > "$tmp"
  mv "$tmp" "$cfg"
  log "lefthook wired — commit $cfg and run 'lefthook install' (or let it regenerate)"
}

# ----------------------------------------------------------------------------------
# husky: insert ONE guarded dispatcher line into .husky/<event>, right AFTER the shebang
# (NOT appended). Husky v9 runs the file directly; an existing husky script may end in
# `exit 0`, which would make a trailing append dead code — so we insert near the top, the
# same way as raw hooks. The repo's own commands (lint-staged etc.) are preserved below.
# ----------------------------------------------------------------------------------
inject_husky() {
  wired_any=0
  for ev in $EVENTS; do
    f="$REPO/.husky/$ev"
    # Only wire events that already have a husky script, plus always-create pre-commit.
    if [ ! -f "$f" ] && [ "$ev" != "pre-commit" ]; then continue; fi
    if [ -f "$f" ] && grep -q "$MARKER" "$f"; then
      log "husky $ev already wired — no-op"; wired_any=1; continue
    fi
    cmt="# $MARKER — runs every global hook for this event (see agent-tools git-hooks/global-dispatcher)"
    if [ "$ev" = "commit-msg" ]; then
      call="$DISPATCHER commit-msg \"\$1\" || exit \$?"
    else
      call="$DISPATCHER $ev \"\$@\" || exit \$?"
    fi
    if [ ! -f "$f" ]; then
      # Fresh husky hook (e.g. a repo that has .husky/ but no commit-msg yet).
      printf '#!/bin/sh\n%s\n%s\n' "$cmt" "$call" > "$f"
    else
      # Insert after the first line (the shebang) so a trailing `exit 0` can't shadow it.
      # If the file has no shebang, awk still inserts after line 1 — harmless, husky runs
      # it via sh either way. Pass the two lines as SEPARATE awk vars (no literal newline).
      tmp="$(mktemp "${TMPDIR:-/tmp}/huskyhook.XXXXXX")"
      awk -v cmt="$cmt" -v call="$call" 'NR==1{print; print cmt; print call; next} {print}' "$f" > "$tmp"
      mv "$tmp" "$f"
    fi
    chmod +x "$f"
    log "husky $ev wired ($f)"
    wired_any=1
  done
  [ "$wired_any" = 1 ] || log "husky: nothing to wire"
}

# ----------------------------------------------------------------------------------
# raw .git/hooks: chain the dispatcher onto each event hook without clobbering an
# existing script. The dispatcher call is INSERTED right after the shebang, NOT
# appended — an existing hook may end in `exit 0` / `set -e` exit, which would make a
# trailing append dead code. Running the dispatcher first also means the secret-scan
# fires before the repo's (possibly slow) lint/test gate.
# ----------------------------------------------------------------------------------
inject_raw() {
  hooks_dir="$REPO/.git/hooks"
  # Honour a local core.hooksPath if the repo set one to a non-default dir.
  local_hp="$(git -C "$REPO" config --local core.hooksPath 2>/dev/null || true)"
  case "$local_hp" in
    ""|"$REPO/.git/hooks"|".git/hooks") : ;;       # default; leave hooks_dir
    /*) hooks_dir="$local_hp" ;;
    *)  hooks_dir="$REPO/$local_hp" ;;
  esac
  mkdir -p "$hooks_dir"
  for ev in $EVENTS; do
    f="$hooks_dir/$ev"
    if [ -f "$f" ] && grep -q "$MARKER" "$f"; then
      log "raw $ev already wired — no-op"; continue
    fi
    # Build the guarded call line for this event (two physical lines: comment + call).
    cmt="# $MARKER — runs every global hook for this event (agent-tools git-hooks/global-dispatcher)"
    if [ "$ev" = "commit-msg" ]; then
      call="$DISPATCHER commit-msg \"\$1\" || exit \$?"
    else
      call="$DISPATCHER $ev \"\$@\" || exit \$?"
    fi
    if [ ! -f "$f" ]; then
      # Fresh hook: shebang + dispatcher call.
      printf '#!/bin/sh\n%s\n%s\nexit 0\n' "$cmt" "$call" > "$f"
    else
      # Existing hook: insert the block right after the shebang (first line) so it
      # runs even if the body later `exit`s. awk preserves the rest verbatim.
      # Pass the two lines as SEPARATE awk vars — `-v` forbids literal newlines.
      tmp="$(mktemp "${TMPDIR:-/tmp}/rawhook.XXXXXX")"
      awk -v cmt="$cmt" -v call="$call" 'NR==1{print; print cmt; print call; next} {print}' "$f" > "$tmp"
      mv "$tmp" "$f"
    fi
    chmod +x "$f"
    log "raw $ev wired ($f)"
  done
}

manager="$(detect_manager)"
log "detected manager: $manager"
case "$manager" in
  lefthook) inject_lefthook ;;
  husky)    inject_husky ;;
  raw)      inject_raw ;;
esac
log "done"
