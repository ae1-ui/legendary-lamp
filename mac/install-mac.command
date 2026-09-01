#!/bin/bash
#
# 워터마크 툴 - macOS 설치 스크립트 (처음 한 번만 실행)
#
#   · 파이썬과 필요한 부품이 있는지 확인하고 없으면 설치합니다.
#   · "워터마크 툴.app" 과 "워터마크 툴 종료.app" 을 프로젝트 폴더에 만듭니다.
#   · 만들어진 앱은 터미널 창 없이 더블클릭으로 실행됩니다.
#
# 실행 방법: 이 파일을 더블클릭하거나, 터미널에서
#            bash "이 파일 경로"
#
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_NAME="워터마크 툴"
QUIT_NAME="워터마크 툴 종료"

say()  { printf '%s\n' "$*"; }
step() { printf '\n▶ %s\n' "$*"; }
ok()   { printf '   ✓ %s\n' "$*"; }
warn() { printf '   ! %s\n' "$*"; }
die()  { printf '\n✗ %s\n\n' "$*"; printf '창을 닫으셔도 됩니다.\n'; exit 1; }

say "────────────────────────────────────────────"
say "  워터마크 툴 · 맥 설치"
say "────────────────────────────────────────────"
say "프로그램 폴더: $PROJECT_DIR"

[ -f "$PROJECT_DIR/app.py" ] || die "이 스크립트는 프로그램 폴더 안의 mac 폴더에 있어야 합니다.
app.py 를 찾을 수 없습니다: $PROJECT_DIR"

if [ "$(uname -s)" != "Darwin" ]; then
    warn "맥이 아닌 환경입니다. 앱 파일은 만들어지지만 더블클릭 실행은 맥에서만 동작합니다."
fi

# ---------------------------------------------------------------- 1. 파이썬 찾기
step "1/4  파이썬 확인"

PYTHON_BIN=""
for candidate in \
    "$(command -v python3 2>/dev/null)" \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 \
    /usr/bin/python3
do
    [ -n "$candidate" ] && [ -x "$candidate" ] || continue
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
        PYTHON_BIN="$candidate"
        break
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    command -v open >/dev/null 2>&1 && open "https://www.python.org/downloads/"
    die "파이썬(Python 3.9 이상)이 설치되어 있지 않습니다.

python.org 다운로드 페이지를 열었습니다.
노란 Download Python 버튼으로 설치한 뒤, 이 설치 파일을 다시 실행해 주세요."
fi
ok "$("$PYTHON_BIN" -V 2>&1)  ($PYTHON_BIN)"

# ---------------------------------------------------------------- 2. 부품 설치
step "2/4  필요한 부품 설치 (1~3분 걸릴 수 있습니다)"

if ! "$PYTHON_BIN" -m pip install --upgrade pip >/dev/null 2>&1; then
    warn "pip 업그레이드는 건너뜁니다."
fi

if ! "$PYTHON_BIN" -m pip install -r "$PROJECT_DIR/requirements.txt"; then
    die "부품 설치에 실패했습니다.
인터넷 연결을 확인한 뒤 다시 실행해 주세요."
fi

"$PYTHON_BIN" -c 'import streamlit, PIL, numpy' 2>/dev/null \
    || die "설치는 끝났지만 부품을 불러오지 못했습니다. 맥을 다시 시작한 뒤 한 번 더 시도해 주세요."
ok "streamlit · Pillow · numpy 준비 완료"

if "$PYTHON_BIN" -c 'import streamlit_cropper' 2>/dev/null; then
    ok "streamlit-cropper (마우스 드래그 크롭) 준비 완료"
else
    warn "streamlit-cropper 를 설치하지 못했습니다. 크롭은 X/Y 슬라이더로 대신 조정됩니다."
fi

# ---------------------------------------------------------------- 3. 앱 만들기
step "3/4  실행 앱 만들기"

# 맥에서는 애플 기본 도구인 osacompile 로 앱을 만든다.
# 직접 조립한 앱 번들은 서명이 없어 Finder 가 실행을 거부하는 경우가 있는데,
# osacompile 이 만든 앱은 애플이 서명한 실행 파일(applet)을 쓰므로 그냥 열린다.
make_bundle_applescript() {   # $1 = 앱 이름, $3 = 실행 스크립트 파일
    local name="$1" script="$3"
    local bundle="$PROJECT_DIR/$name.app"
    rm -rf "$bundle"

    cat > "$TMP_DIR/applet.applescript" <<'APPLESCRIPT'
on run
    try
        set appPath to POSIX path of (path to me)
        with timeout of 300 seconds
            do shell script "/bin/bash " & quoted form of (appPath & "Contents/Resources/run.sh")
        end timeout
    end try
end run
APPLESCRIPT

    osacompile -o "$bundle" "$TMP_DIR/applet.applescript" || return 1
    cp "$script" "$bundle/Contents/Resources/run.sh"
    chmod 755 "$bundle/Contents/Resources/run.sh"
    [ -f "$PROJECT_DIR/mac/icon.icns" ] && cp "$PROJECT_DIR/mac/icon.icns" "$bundle/Contents/Resources/applet.icns"
    xattr -cr "$bundle" 2>/dev/null
    touch "$bundle"
    ok "$name.app"
    return 0
}

make_bundle_manual() {    # osacompile 이 없는 환경(테스트용)에서 쓰는 예비 방식
    local name="$1" desc="$2" script="$3"
    local bundle="$PROJECT_DIR/$name.app"
    rm -rf "$bundle"
    mkdir -p "$bundle/Contents/MacOS" "$bundle/Contents/Resources"

    cat > "$bundle/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>                 <string>$name</string>
    <key>CFBundleDisplayName</key>          <string>$name</string>
    <key>CFBundleExecutable</key>           <string>run</string>
    <key>CFBundleIdentifier</key>           <string>local.watermarktool.$(echo "$desc" | tr -cd 'a-z')</string>
    <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
    <key>CFBundlePackageType</key>          <string>APPL</string>
    <key>CFBundleShortVersionString</key>   <string>1.0</string>
    <key>CFBundleVersion</key>              <string>1</string>
    <key>CFBundleIconFile</key>             <string>icon</string>
    <key>LSMinimumSystemVersion</key>       <string>10.13</string>
    <key>LSUIElement</key>                  <true/>
    <key>NSHighResolutionCapable</key>      <true/>
</dict>
</plist>
PLIST

    cp "$script" "$bundle/Contents/MacOS/run"
    chmod 755 "$bundle/Contents/MacOS/run"

    # 다운로드 표식(격리 속성)을 떼어 "확인되지 않은 개발자" 경고를 막는다
    command -v xattr >/dev/null 2>&1 && xattr -cr "$bundle" 2>/dev/null
    command -v codesign >/dev/null 2>&1 && codesign --force --sign - "$bundle" >/dev/null 2>&1
    command -v touch >/dev/null 2>&1 && touch "$bundle"
    [ -f "$PROJECT_DIR/mac/icon.icns" ] && cp "$PROJECT_DIR/mac/icon.icns" "$bundle/Contents/Resources/icon.icns"
    ok "$name.app  (예비 방식)"
}

make_bundle() {
    if command -v osacompile >/dev/null 2>&1 && make_bundle_applescript "$@"; then
        return 0
    fi
    make_bundle_manual "$@"
}

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

# --- 실행 스크립트 ---------------------------------------------------------
cat > "$TMP_DIR/run-launch" <<'LAUNCHER'
#!/bin/bash
# 워터마크 툴 실행기 - 터미널 창 없이 Streamlit 을 띄우고 브라우저를 연다.
set -u

PROJECT_DIR="@@PROJECT_DIR@@"
PYTHON_BIN="@@PYTHON_BIN@@"
APP_TITLE="워터마크 툴"
PORT_START=8501
PORT_END=8510
LOG_FILE="$PROJECT_DIR/.watermark-tool.log"
PID_FILE="$PROJECT_DIR/.watermark-tool.pid"
PORT_FILE="$PROJECT_DIR/.watermark-tool.port"

have() { command -v "$1" >/dev/null 2>&1; }

alert() {   # 사용자에게 보이는 오류창 (맥). 맥이 아니면 표준 출력.
    if have osascript; then
        /usr/bin/osascript -e "display dialog \"$1\" buttons {\"확인\"} default button 1 with title \"$APP_TITLE\" with icon caution" >/dev/null 2>&1
    else
        printf 'ALERT: %s\n' "$1" >&2
    fi
}

open_url() {
    if have open; then open "$1"; else printf 'OPEN %s\n' "$1"; fi
}

healthy() {   # $1 = 포트. Streamlit 이 응답하면 0
    have curl || return 1
    curl -fsS -o /dev/null --max-time 2 "http://127.0.0.1:$1/_stcore/health" 2>/dev/null
}

port_free() {  # $1 = 포트. 아무도 쓰고 있지 않으면 0
    "$PYTHON_BIN" -c '
import socket, sys
s = socket.socket()
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(1)
finally:
    s.close()
' "$1" 2>/dev/null
}

running_pid() {   # 우리가 띄운 서버가 살아 있으면 PID 출력
    [ -f "$PID_FILE" ] || return 1
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null)"
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    printf '%s' "$pid"
}

# ---------- 1. 이미 실행 중이면 브라우저만 연다 ----------
if running_pid >/dev/null && [ -f "$PORT_FILE" ]; then
    port="$(cat "$PORT_FILE" 2>/dev/null)"
    if [ -n "$port" ] && healthy "$port"; then
        open_url "http://localhost:$port"
        exit 0
    fi
fi

# ---------- 2. 준비 상태 확인 ----------
if [ ! -f "$PROJECT_DIR/app.py" ]; then
    alert "프로그램 폴더를 찾을 수 없습니다.

찾는 곳: $PROJECT_DIR

폴더를 옮기셨다면 mac 폴더의 install-mac.command 를 다시 실행해 주세요."
    exit 1
fi

if [ ! -x "$PYTHON_BIN" ]; then
    for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
        [ -x "$candidate" ] && PYTHON_BIN="$candidate" && break
    done
fi
if [ ! -x "$PYTHON_BIN" ]; then
    alert "파이썬을 찾을 수 없습니다.

mac 폴더의 install-mac.command 를 다시 실행해 주세요."
    exit 1
fi

if ! "$PYTHON_BIN" -c 'import streamlit' 2>/dev/null; then
    alert "실행에 필요한 부품이 설치되어 있지 않습니다.

mac 폴더의 install-mac.command 를 한 번 실행해 주세요."
    exit 1
fi

# ---------- 3. 빈 포트를 찾아 서버 시작 ----------
port=""
for candidate in $(seq "$PORT_START" "$PORT_END"); do
    if port_free "$candidate"; then port="$candidate"; break; fi
done
if [ -z "$port" ]; then
    alert "사용할 수 있는 포트가 없습니다 ($PORT_START~$PORT_END).

'워터마크 툴 종료' 를 한 번 실행한 뒤 다시 시도해 주세요."
    exit 1
fi

cd "$PROJECT_DIR" || exit 1
: > "$LOG_FILE"
nohup "$PYTHON_BIN" -m streamlit run app.py \
    --server.port "$port" \
    --server.address localhost \
    --server.headless true \
    --browser.gatherUsageStats false \
    >>"$LOG_FILE" 2>&1 &
printf '%s' "$!" > "$PID_FILE"
printf '%s' "$port" > "$PORT_FILE"

# ---------- 4. 준비될 때까지 기다렸다가 브라우저 열기 ----------
for _ in $(seq 1 80); do          # 최대 40초
    if healthy "$port"; then
        open_url "http://localhost:$port"
        exit 0
    fi
    if ! kill -0 "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null; then
        break
    fi
    sleep 0.5
done

alert "프로그램을 시작하지 못했습니다.

$(tail -n 6 "$LOG_FILE" 2>/dev/null)

mac 폴더의 install-mac.command 를 다시 실행해 보세요."
exit 1
LAUNCHER

# --- 종료 스크립트 ---------------------------------------------------------
cat > "$TMP_DIR/run-quit" <<'QUITTER'
#!/bin/bash
# 워터마크 툴 종료기 - 실행 중인 Streamlit 서버를 안전하게 끈다.
set -u

PROJECT_DIR="@@PROJECT_DIR@@"
APP_TITLE="워터마크 툴"
PID_FILE="$PROJECT_DIR/.watermark-tool.pid"
PORT_FILE="$PROJECT_DIR/.watermark-tool.port"

have() { command -v "$1" >/dev/null 2>&1; }

notify() {   # 잠깐 떴다 사라지는 알림창
    if have osascript; then
        /usr/bin/osascript -e "display dialog \"$1\" buttons {\"확인\"} default button 1 with title \"$APP_TITLE\" giving up after 3" >/dev/null 2>&1
    else
        printf 'NOTIFY: %s\n' "$1"
    fi
}

stopped=0

stop_pid() {   # $1 = PID. 먼저 얌전히, 안 되면 강제로.
    local pid="$1"
    kill -0 "$pid" 2>/dev/null || return 1
    kill -TERM "$pid" 2>/dev/null
    for _ in $(seq 1 20); do            # 최대 10초 기다림
        kill -0 "$pid" 2>/dev/null || return 0
        sleep 0.5
    done
    kill -KILL "$pid" 2>/dev/null
    return 0
}

# 1) 우리가 기록해 둔 PID
if [ -f "$PID_FILE" ]; then
    pid="$(cat "$PID_FILE" 2>/dev/null)"
    if [ -n "$pid" ] && stop_pid "$pid"; then stopped=$((stopped + 1)); fi
fi

# 2) 혹시 남아 있는 같은 폴더의 Streamlit 프로세스
#    "작업 폴더가 정확히 이 프로젝트 폴더인 것" 만 끈다.
#    명령줄 글자만 보고 판단하면 우연히 같은 글자를 가진 다른 프로그램을 끌 수 있다.
process_cwd() {   # $1 = PID 의 현재 작업 폴더
    if [ -r "/proc/$1/cwd" ]; then
        readlink "/proc/$1/cwd" 2>/dev/null
    elif have lsof; then
        lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1
    fi
}

process_is_python() {   # 진짜 파이썬 프로세스인지 (명령줄에 같은 글자가 있는 셸 등을 걸러낸다)
    local comm
    comm="$(ps -o comm= -p "$1" 2>/dev/null | tr -d ' ')"
    case "$(basename "${comm:-none}")" in
        python|python3|python3.*|Python) return 0 ;;
        *) return 1 ;;
    esac
}

if have pgrep; then
    for pid in $(pgrep -f -- "-m streamlit run app\.py" 2>/dev/null); do
        [ "$pid" = "$$" ] && continue
        [ "$pid" = "${PPID:-0}" ] && continue
        process_is_python "$pid" || continue
        [ "$(process_cwd "$pid")" = "$PROJECT_DIR" ] || continue
        stop_pid "$pid" && stopped=$((stopped + 1))
    done
fi

rm -f "$PID_FILE" "$PORT_FILE"

if [ "$stopped" -gt 0 ]; then
    notify "워터마크 툴을 종료했습니다.

열려 있는 브라우저 탭은 닫아 주세요."
else
    notify "실행 중인 워터마크 툴이 없습니다."
fi
exit 0
QUITTER

# 경로를 스크립트 안에 새겨 넣는다 (앱을 다른 곳으로 옮겨도 동작하도록)
"$PYTHON_BIN" - "$TMP_DIR/run-launch" "$TMP_DIR/run-quit" "$PROJECT_DIR" "$PYTHON_BIN" <<'SUBST'
import pathlib, sys
launch, quit_, project, python = sys.argv[1:5]
for path in (launch, quit_):
    p = pathlib.Path(path)
    p.write_text(p.read_text(encoding="utf-8")
                 .replace("@@PROJECT_DIR@@", project)
                 .replace("@@PYTHON_BIN@@", python), encoding="utf-8")
SUBST

# 워터마크 이미지로 아이콘 만들기 (맥에서만, 실패해도 그냥 넘어간다)
if command -v sips >/dev/null 2>&1 && command -v iconutil >/dev/null 2>&1 \
   && [ -f "$PROJECT_DIR/watermark.png" ]; then
    ICONSET="$TMP_DIR/icon.iconset"
    mkdir -p "$ICONSET"
    if sips -s format png -z 512 512 -p 512 512 "$PROJECT_DIR/watermark.png" \
            --out "$TMP_DIR/square.png" >/dev/null 2>&1; then
        for size in 16 32 128 256 512; do
            sips -z "$size" "$size" "$TMP_DIR/square.png" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null 2>&1
        done
        iconutil -c icns "$ICONSET" -o "$PROJECT_DIR/mac/icon.icns" >/dev/null 2>&1 || true
    fi
fi

make_bundle "$APP_NAME" "launcher" "$TMP_DIR/run-launch"
make_bundle "$QUIT_NAME" "quitter" "$TMP_DIR/run-quit"

# ---------------------------------------------------------------- 4. 마무리
step "4/4  마무리"
command -v xattr >/dev/null 2>&1 && xattr -cr "$PROJECT_DIR" 2>/dev/null
ok "완료"

say ""
say "────────────────────────────────────────────"
say "  설치가 끝났습니다"
say "────────────────────────────────────────────"
say ""
say "프로그램 폴더에 아래 두 개가 생겼습니다."
say ""
say "   ▶  $APP_NAME.app          더블클릭 → 프로그램 실행"
say "   ■  $QUIT_NAME.app     더블클릭 → 프로그램 종료"
say ""
say "자주 쓰시려면 '$APP_NAME.app' 을 독(Dock)으로 끌어다 놓으세요."
say ""
say "혹시 더블클릭해도 안 열리면, 터미널에 아래를 붙여 넣어 무슨 일인지 확인할 수 있습니다."
say "   open \"$PROJECT_DIR/$APP_NAME.app\""
say ""
command -v open >/dev/null 2>&1 && open "$PROJECT_DIR"
say "이 창은 닫으셔도 됩니다."
