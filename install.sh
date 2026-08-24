#!/usr/bin/env sh
set -e
echo "=== Quota CLI 安装 ==="

if ! command -v python3 >/dev/null 2>&1; then
    echo "[错误] 未找到 python3，请先安装 Python 3.10+"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
python3 -m pip install -r "$SCRIPT_DIR/requirements.txt" --quiet

BINDIR="$HOME/.local/bin"
mkdir -p "$BINDIR"
ln -sf "$SCRIPT_DIR/quota.py" "$BINDIR/quota.py"
cat > "$BINDIR/quota" <<EOF
#!/usr/bin/env sh
exec python3 "$BINDIR/quota.py" "\$@"
EOF
chmod +x "$BINDIR/quota"

echo ""
echo "已安装到 $BINDIR/quota（确认 ~/.local/bin 在 PATH 中）"
echo "交互菜单: quota"
echo "Web UI:   quota ui"
