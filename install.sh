#!/bin/sh
set -e
ROOT=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
echo "=== Dushan Quota install ==="

if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "[error] Python not found. Install Python 3.10+." >&2
  exit 1
fi

"$PY" -c "import sys; raise SystemExit(0 if sys.hexversion >= 0x030A0000 else 1)" || {
  echo "[error] Python 3.10+ required." >&2
  "$PY" --version >&2
  exit 1
}

echo "Installing dependencies..."
"$PY" -m pip install -r "$ROOT/requirements.txt"

BINDIR="$HOME/.local/bin"
mkdir -p "$BINDIR"
cat > "$BINDIR/quota" <<EOF
#!/bin/sh
exec "$PY" "$ROOT/quota.py" "\$@"
EOF
chmod +x "$BINDIR/quota"

case ":$PATH:" in
  *":$BINDIR:"*) ;;
  *)
    SHELL_NAME=$(basename "${SHELL:-sh}")
    case "$SHELL_NAME" in
      zsh) PROFILE="${HOME}/.zshrc" ;;
      bash) PROFILE="${HOME}/.bashrc" ;;
      *) PROFILE="${HOME}/.profile" ;;
    esac
    if [ ! -f "$PROFILE" ] || ! grep -Fqs "$BINDIR" "$PROFILE"; then
      printf '\nexport PATH="%s:$PATH"\n' "$BINDIR" >> "$PROFILE"
      echo "Wrote PATH to $PROFILE"
    fi
    ;;
esac

export PATH="$BINDIR:$PATH"
echo
echo "Done. Open a new terminal and run: quota"
echo "This terminal: $BINDIR/quota"
