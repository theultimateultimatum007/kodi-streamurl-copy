# -*- coding: utf-8 -*-
"""Core logic for the Stream URL Copy add-on."""

import os
import sys

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs


def _translate_path(path):
    # xbmcvfs.translatePath exists on Kodi 19+; xbmc.translatePath was
    # removed in Kodi 20. Fall back gracefully either way.
    if hasattr(xbmcvfs, "translatePath"):
        return xbmcvfs.translatePath(path)
    if hasattr(xbmc, "translatePath"):
        return xbmc.translatePath(path)
    return path


# Make sure our bundled lib folder is importable regardless of entry point.
_ADDON = xbmcaddon.Addon()
_ADDON_PATH = _translate_path(_ADDON.getAddonInfo("path"))

_LIB_PATH = os.path.join(_ADDON_PATH, "resources", "lib")
if _LIB_PATH not in sys.path:
    sys.path.insert(0, _LIB_PATH)

from clipboard import copy_to_clipboard  # noqa: E402


def _(string_id):
    """Return a localized string by id."""
    return _ADDON.getLocalizedString(string_id)


def get_playing_url():
    """Return the URL / path of the currently playing item, or None."""
    player = xbmc.Player()
    if not player.isPlaying():
        return None
    try:
        return player.getPlayingFile()
    except RuntimeError:
        # Playback stopped between the isPlaying() check and this call.
        return None


def _notify(message, icon=xbmcgui.NOTIFICATION_INFO):
    xbmcgui.Dialog().notification(
        _ADDON.getAddonInfo("name"),
        message,
        icon,
        4000,
    )


def run():
    """Entry point: grab the current stream URL and copy it to clipboard."""
    url = get_playing_url()

    if not url:
        _notify(_(30001), xbmcgui.NOTIFICATION_WARNING)  # Nothing is playing
        return

    show_dialog = _ADDON.getSettingBool("show_url_dialog") \
        if hasattr(_ADDON, "getSettingBool") else \
        (_ADDON.getSetting("show_url_dialog") == "true")

    if copy_to_clipboard(url):
        _notify(_(30002))  # Stream URL copied to clipboard
        xbmc.log("[script.streamurlcopy] Copied URL: %s" % url, xbmc.LOGINFO)
    else:
        # Clipboard access failed, at least show the URL so it can be copied.
        _notify(_(30003), xbmcgui.NOTIFICATION_ERROR)  # Could not access clipboard
        xbmc.log("[script.streamurlcopy] Clipboard copy failed for: %s" % url,
                 xbmc.LOGWARNING)
        show_dialog = True

    if show_dialog:
        xbmcgui.Dialog().textviewer(_ADDON.getAddonInfo("name"), url)
