"""Local network-build helpers: progress parsing, exit checks, Wine selection, build script."""
from pathlib import Path
import platform
import re
import subprocess

from build_runtime import wine_candidates


REUSE_MESSAGE = 'Reusing the verified prepared package on this drive; nothing is rebuilt'
COPY_MESSAGE = ('Copying the verified package this device already built on another drive; '
                'nothing is rebuilt and no games are changed')
BUILD_MESSAGE = ('Building the local NVIDIA-derived network. This is needed once per device for this '
                 'HelixSR version and model; other games reuse it. No games are changed.')
BUILD_PREPARATION_NOTE = ('The first preparation of this HelixSR version and model on this device builds its '
                          'network locally. This can take several minutes, longer on a Steam Deck. Other games reuse '
                          'it; a game on another drive, such as a microSD card, gets a verified copy instead of a rebuild.')
HLX_PREPARATION_NOTE = ('HLX FSR 4 needs no local network build: preparation downloads and verifies the reviewed '
                        'files, then other games on this drive reuse them.')
BUILD_IDLE_SECONDS = 600
BUILD_LIMIT_SECONDS = 3600
_BUILD_LINE = re.compile(r'^\[helixsr-(setup|fg)\] (.+)$')
_BUILD_BAR = re.compile(r'^\[[#-]{20}\]\s+(\d{1,3})%\s+\d+:\d\d\s+(.*)$')


def build_progress(log):
    """Latest upstream setup progress for the job card, e.g. 'frame generation 63% (2/4 shaders 120/236)'."""
    for line in reversed(re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', log or '').replace('\r', '\n').splitlines()):
        match = _BUILD_LINE.match(line.strip())
        if not match:
            continue
        stage = 'upscaling network' if match.group(1) == 'setup' else 'frame generation'
        bar = _BUILD_BAR.match(match.group(2))
        if bar:
            label = bar.group(2).strip()
            return stage + ' ' + bar.group(1) + '%' + (' (' + label[:80] + ')' if label else '')
        return stage + ': ' + match.group(2).strip()[:140]
    return 'preparing build tools' if (log or '').strip() else None


def check_build_exit(returncode, cancelled, log=''):
    if cancelled:
        raise ValueError('Cancelled')
    if returncode != 0:
        log = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', log[-8000:])
        if 'no Proton or Wine found' in log:
            detail = 'Proton or Wine was not found. Install Proton through Steam and retry.'
        elif 'Permission denied' in log:
            detail = 'Preparation was denied access. Check the game drive permissions and executable support.'
        elif 'No space left on device' in log:
            detail = 'The game drive ran out of space.'
        else:
            lines = [line.strip() for line in log.splitlines() if line.strip()]
            detail = lines[-1][:320] if lines else ''
        raise ValueError('Network build failed. ' + (detail + ' ' if detail else '') +
                         'See the build log for details.')


def select_build_wine(home, env):
    """Only override upstream discovery with a Steam runtime that starts on this host."""
    if platform.system() != 'Linux':
        return None, []
    notes = []
    for candidate in wine_candidates(home)[:12]:
        try:
            probe = subprocess.run([str(candidate), '--version'], env=env,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True,
                                   encoding='utf-8', errors='replace', timeout=3)
            output = probe.stdout.strip()
            if probe.returncode == 0 and re.search(r'^wine-\S+', output, re.MULTILINE):
                notes.append('Build runtime: ' + str(candidate) + ' (' + output[:120] + ')')
                return candidate, notes
            notes.append('Skipped build runtime: ' + str(candidate) + ': ' + output[:200])
        except (OSError, subprocess.TimeoutExpired) as error:
            notes.append('Skipped build runtime: ' + str(candidate) + ': ' + str(error)[:200])
    return None, notes


def build_only_script(setup, converter, runtime_override=False):
    """Keep local network files separate and stop before upstream game writes."""
    marker = '# --- games ---'
    invocation = '"$PY" "$PKG/helixsr_setup.py" "$TARGET" --dxc "$DXC" "${PASS[@]}"'
    if setup.count(marker) != 1:
        raise ValueError('Upstream setup changed; build-only adapter needs review')
    script = setup.split(marker)[0]
    if script.count(invocation) != 1 or '--separate-files' not in converter:
        raise ValueError('Upstream network output changed; build-only adapter needs review')
    if runtime_override:
        wine_start = 'WINE=""\n# official Proton first, then custom Proton builds, then a system Wine'
        wine_end = '[ -n "$WINE" ] || die "no Proton or Wine found (needed to run the shader compiler); install Proton in Steam or Wine"'
        wrapper_start = 'cat > "$DXC" <<EOF\n'
        wrapper_end = '\nEOF\nchmod +x "$DXC"'
        wrapper_command = 'WINEPREFIX="$DATA/wineprefix" WINEDEBUG=-all exec "$WINE" "$DATA/dxc-win/bin/x64/dxc.exe"'
        if (script.count(wine_start) != 1 or script.count(wine_end) != 1 or
                script.count(wrapper_start) != 1 or script.count(wrapper_end) != 1 or
                script.count(wrapper_command) != 1):
            raise ValueError('Upstream compiler runtime changed; build-only adapter needs review')
        script = script.replace(wine_start, 'WINE="${HELIXSR_BUILD_WINE:-}"\n'
                                'if [ -n "$WINE" ]; then\n'
                                '    [ -x "$WINE" ] || die "selected Proton or Wine is not executable"\n'
                                'else\n' + wine_start)
        script = script.replace(wine_end, wine_end + '\nfi')
        # A quoted heredoc plus inherited environment keeps arbitrary library
        # and companion paths out of shell source, including quotes and dollars.
        begin = script.index(wrapper_start)
        end = script.index(wrapper_end, begin) + len(wrapper_end)
        wrapper = r'''export HELIXSR_BUILD_WINE="$WINE" HELIXSR_BUILD_DATA="$DATA"
cat > "$DXC" <<'HELIXSR_DECKY_DXC'
#!/usr/bin/env bash
args=()
for a in "$@"; do
    case "$a" in
        /*) args+=("Z:$(printf '%s' "$a" | tr / '\\')") ;;
        *) args+=("$a") ;;
    esac
done
WINEPREFIX="$HELIXSR_BUILD_DATA/wineprefix" WINEDEBUG=-all exec "$HELIXSR_BUILD_WINE" "$HELIXSR_BUILD_DATA/dxc-win/bin/x64/dxc.exe" "${args[@]}"
HELIXSR_DECKY_DXC
chmod +x "$DXC"'''
        script = script[:begin] + wrapper + script[end:]
    # 1.8 embeds the network into its DLL and removes the separate files by default.
    # Request its supported separate output at the converter, not the shell CLI.
    return script.replace(invocation, invocation + ' --separate-files') + '\n'
