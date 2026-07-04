# -*- coding: utf-8 -*-
"""Cross-platform clipboard helpers for the Stream URL Copy add-on.

Kodi's bundled Python does not expose a clipboard API, so we implement one
per platform:

* Windows  -> native Win32 clipboard via ctypes (no external deps)
* macOS    -> the ``pbcopy`` command line tool
* Linux    -> ``wl-copy`` on Wayland, ``xclip`` / ``xsel`` on X11 (each is
              also tried as a fallback for the other).

``copy_to_clipboard`` returns ``True`` on success and ``False`` otherwise so
the caller can show a meaningful notification to the user.
"""

import os
import subprocess
import sys


def _log(message):
    """Best-effort logging to the Kodi log (no-op outside Kodi)."""
    try:
        import xbmc
        xbmc.log("[script.streamurlcopy] %s" % message, xbmc.LOGINFO)
    except Exception:
        pass


def copy_to_clipboard(text):
    """Copy ``text`` to the system clipboard.

    Returns True on success, False on failure.
    """
    if text is None:
        return False

    text = str(text)

    if sys.platform.startswith("win"):
        return _copy_windows(text)
    if sys.platform == "darwin":
        return _copy_macos(text)
    return _copy_linux(text)


def _copy_windows(text):
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False

    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32

    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]

    # Size in bytes for a null-terminated UTF-16 (wide char) string.
    data = text + "\0"
    size = len(data) * ctypes.sizeof(ctypes.c_wchar)

    if not user32.OpenClipboard(None):
        return False
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            return False
        locked = kernel32.GlobalLock(handle)
        if not locked:
            return False
        try:
            ctypes.memmove(locked, ctypes.create_unicode_buffer(data), size)
        finally:
            kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            return False
        # Ownership of the memory now belongs to the clipboard.
        return True
    finally:
        user32.CloseClipboard()


def _copy_macos(text):
    ok, _ = _pipe_to_command(["pbcopy"], text)
    return ok


def _is_flatpak():
    """True if this Kodi is running inside a Flatpak sandbox."""
    return os.path.exists("/.flatpak-info") or bool(os.environ.get("FLATPAK_ID"))


def _resolve(name):
    """Return an absolute path to ``name``, searching common bin dirs too.

    Kodi launchers (desktop entry / systemd) sometimes start with a stripped
    PATH, so a plain command name may not be found even when installed.
    """
    from shutil import which
    found = which(name)
    if found:
        return found
    for directory in ("/usr/bin", "/bin", "/usr/local/bin", "/sbin"):
        candidate = os.path.join(directory, name)
        if os.path.exists(candidate):
            return candidate
    return None


def _copy_linux(text):
    session = os.environ.get("XDG_SESSION_TYPE", "").lower()
    is_wayland = bool(os.environ.get("WAYLAND_DISPLAY")) or session == "wayland"
    flatpak = _is_flatpak()

    wayland_cmd = ["wl-copy"]
    x11_cmds = [
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
    ]

    # Prefer the native tool for the running session, but still try the others
    # afterwards: XWayland lets X11 tools work, and some setups misreport
    # XDG_SESSION_TYPE.
    if is_wayland:
        base = [wayland_cmd] + x11_cmds
    else:
        base = x11_cmds + [wayland_cmd]

    candidates = []
    if flatpak:
        # Inside a Flatpak sandbox the host's clipboard tools are not visible,
        # so run them on the host through the Flatpak portal. This requires the
        # 'org.freedesktop.Flatpak' talk permission (see README).
        candidates += [["flatpak-spawn", "--host"] + cmd for cmd in base]
    # Also try directly (native install, or the tool present inside the sandbox).
    candidates += base

    _log("Linux clipboard: session=%r wayland=%s flatpak=%s DISPLAY=%r "
         "WAYLAND_DISPLAY=%r" % (session, is_wayland, flatpak,
                                 os.environ.get("DISPLAY"),
                                 os.environ.get("WAYLAND_DISPLAY")))

    errors = []
    for cmd in candidates:
        ok, err = _pipe_to_command(cmd, text)
        if ok:
            _log("Copied to clipboard using: %s" % " ".join(cmd))
            return True
        if err:
            errors.append("%s -> %s" % (" ".join(cmd), err))

    if errors:
        _log("All clipboard attempts failed: %s" % "; ".join(errors))
    else:
        _log("No clipboard tool found. Install 'wl-clipboard' (Wayland) or "
             "'xclip'/'xsel' (X11): sudo pacman -S wl-clipboard")
    return False


def _linux_env():
    """Return an environment with the display variables filled in.

    Kodi sometimes spawns add-on subprocesses without WAYLAND_DISPLAY /
    DISPLAY, which makes wl-copy and xclip unable to reach the compositor.
    """
    env = dict(os.environ)
    if not env.get("WAYLAND_DISPLAY") and not env.get("DISPLAY"):
        runtime = env.get("XDG_RUNTIME_DIR")
        wayland_sock = os.path.join(runtime, "wayland-0") if runtime else None
        if wayland_sock and os.path.exists(wayland_sock):
            env["WAYLAND_DISPLAY"] = "wayland-0"
        else:
            env["DISPLAY"] = ":0"
    return env


def _pipe_to_command(cmd, text):
    """Feed ``text`` to ``cmd`` via stdin. Returns (success, error_message)."""
    is_windows = sys.platform.startswith("win")

    # Resolve the executable to an absolute path (handles stripped PATH). For
    # 'flatpak-spawn --host <tool>' only the launcher is resolved locally; the
    # tool itself is looked up on the host, so leave its name untouched.
    if not is_windows and cmd:
        resolved = _resolve(cmd[0])
        if resolved is None:
            return False, "not installed"
        cmd = [resolved] + cmd[1:]

    env = None if is_windows else _linux_env()
    popen_kwargs = dict(
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=env,
    )
    # Detach the clipboard-holder process (wl-copy/xclip stay resident to
    # serve the selection) so Kodi does not kill it when the add-on exits.
    if not is_windows:
        popen_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(cmd, **popen_kwargs)
    except FileNotFoundError:
        return False, "not installed"
    except (OSError, ValueError) as exc:
        return False, str(exc)

    try:
        _, stderr = proc.communicate(input=text.encode("utf-8"), timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        return False, "timed out"
    except Exception as exc:
        return False, str(exc)

    if proc.returncode == 0:
        return True, ""

    detail = stderr.decode("utf-8", "replace").strip() if stderr else ""
    return False, "exit %s%s" % (proc.returncode,
                                 ": " + detail if detail else "")
