# -*- coding: utf-8 -*-
"""Cross-platform clipboard helpers for the Stream URL Copy add-on.

Kodi's bundled Python does not expose a clipboard API, so we implement one
per platform:

* Windows  -> native Win32 clipboard via ctypes (no external deps)
* macOS    -> the ``pbcopy`` command line tool
* Linux    -> tries several methods in order and uses the first that works:
              KDE Klipper over D-Bus (no extra packages on Plasma),
              ``wl-copy`` on Wayland, and ``xclip`` / ``xsel`` on X11.

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
    ok, _ = _run_command(["pbcopy"], stdin_text=text)
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


def _linux_methods(text, is_wayland):
    """Build the list of clipboard methods to try on Linux.

    Each method has a ``set`` command that writes the clipboard and a ``read``
    command that reads it back so we can *verify* the copy actually happened
    (some tools, notably Klipper via dbus-send, report success without
    changing the clipboard). ``stdin`` is the text piped to the setter, or
    None when the text is passed as an argument instead.
    """
    wl = {
        "name": "wl-copy",
        "set": ["wl-copy"], "stdin": text,
        "read": ["wl-paste", "--no-newline"], "dbus": False,
    }
    xclip = {
        "name": "xclip",
        "set": ["xclip", "-selection", "clipboard"], "stdin": text,
        "read": ["xclip", "-selection", "clipboard", "-o"], "dbus": False,
    }
    xsel = {
        "name": "xsel",
        "set": ["xsel", "--clipboard", "--input"], "stdin": text,
        "read": ["xsel", "--clipboard", "--output"], "dbus": False,
    }
    klipper = {
        "name": "klipper",
        "set": ["dbus-send", "--type=method_call", "--dest=org.kde.klipper",
                "/klipper", "org.kde.klipper.klipper.setClipboardContents",
                "string:" + text],
        "stdin": None,
        "read": ["dbus-send", "--print-reply", "--dest=org.kde.klipper",
                 "/klipper", "org.kde.klipper.klipper.getClipboardContents"],
        "dbus": True,
    }

    # Prefer the real CLI tool for the session (its read-back reflects the
    # actual system clipboard). Klipper is a last-resort fallback for Plasma
    # boxes without wl-clipboard/xclip installed.
    if is_wayland:
        return [wl, xclip, xsel, klipper]
    return [xclip, xsel, wl, klipper]


def _copy_linux(text):
    session = os.environ.get("XDG_SESSION_TYPE", "").lower()
    is_wayland = bool(os.environ.get("WAYLAND_DISPLAY")) or session == "wayland"
    flatpak = _is_flatpak()

    methods = _linux_methods(text, is_wayland)
    if flatpak:
        # Inside a Flatpak sandbox the host's tools are not visible, so run both
        # the setter and reader on the host through the portal (needs the
        # 'org.freedesktop.Flatpak' talk permission, see README).
        host = ["flatpak-spawn", "--host"]
        for m in methods:
            m["set"] = host + m["set"]
            m["read"] = host + m["read"]

    _log("Linux clipboard: session=%r wayland=%s flatpak=%s DISPLAY=%r "
         "WAYLAND_DISPLAY=%r" % (session, is_wayland, flatpak,
                                 os.environ.get("DISPLAY"),
                                 os.environ.get("WAYLAND_DISPLAY")))

    errors = []
    for m in methods:
        ok, err = _run_command(m["set"], stdin_text=m["stdin"])
        if not ok:
            if err:
                errors.append("%s -> %s" % (m["name"], err))
            continue
        if _verify_clipboard(m["read"], text, m["dbus"]):
            _log("Copied and verified using '%s'" % m["name"])
            return True
        errors.append("%s -> set ok but read-back did not match" % m["name"])

    if errors:
        _log("Clipboard copy failed: %s" % "; ".join(errors))
    else:
        _log("No clipboard tool found. Install 'wl-clipboard' (Wayland) or "
             "'xclip'/'xsel' (X11): sudo pacman -S wl-clipboard")
    return False


def _dbus_value(output):
    """Extract the string payload from a ``dbus-send --print-reply`` reply."""
    marker = 'string "'
    start = output.find(marker)
    if start != -1:
        start += len(marker)
        end = output.rfind('"')
        if end > start:
            return output[start:end]
    return output.strip()


def _verify_clipboard(read_cmd, expected, dbus):
    """Read the clipboard back and confirm it matches ``expected``."""
    rc, out = _capture_command(read_cmd)
    if rc != 0:
        return False
    value = _dbus_value(out) if dbus else out
    return value.rstrip("\r\n") == expected.rstrip("\r\n")


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


def _capture_command(cmd):
    """Run ``cmd`` and capture stdout. Returns (returncode, stdout_text).

    On failure to launch, returns (None, "").
    """
    is_windows = sys.platform.startswith("win")

    if not is_windows and cmd:
        resolved = _resolve(cmd[0])
        if resolved is None:
            return None, ""
        cmd = [resolved] + cmd[1:]

    env = None if is_windows else _linux_env()
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        out, _ = proc.communicate(timeout=5)
    except Exception:
        return None, ""
    return proc.returncode, out.decode("utf-8", "replace") if out else ""


def _run_command(cmd, stdin_text=None):
    """Run ``cmd``, optionally feeding ``stdin_text`` on stdin.

    Returns (success, error_message).
    """
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
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
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

    stdin_bytes = stdin_text.encode("utf-8") if stdin_text is not None else None
    try:
        _, stderr = proc.communicate(input=stdin_bytes, timeout=5)
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
