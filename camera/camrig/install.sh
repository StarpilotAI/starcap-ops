#!/usr/bin/env bash
# camrig + CamView (live UVC tuning in the browser, and the native 1:1 viewer) installer.
#   ./install.sh      (NO_APT=1 to only report missing packages)
#
# Also binds CamView to the ± key (GNOME). CamView is a GTK4/libadwaita app, so
# OpenCV is only used to decode frames there and a headless OpenCV is fine.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../../lib/install_common.sh"
echo "== camrig"
need_cmd python3   python3
need_py  cv2       python3-opencv
need_py  numpy     python3-numpy
need_cmd v4l2-ctl  v4l-utils
need_cmd ffmpeg    ffmpeg
need_cmd fuser     psmisc          # names who holds the camera when it won't open
need_py  gi        python3-gi
need_py  gi.repository.Adw gir1.2-adw-1   # CamView's window (pulls in GTK 4)
need_cmd notify-send libnotify-bin # the hotkey has no terminal to report errors to
need_cmd wmctrl    wmctrl          # the hotkey raises an open viewer instead of a rival
install_deps
chmod +x "$HERE"/*.sh "$HERE"/*.py

rm -rf "${HERE:?}/.venv"   # the old Qt-window OpenCV venv; it cannot import GTK

# A desktop login usually gets camera access without the 'video' group, so only
# complain about a camera node that is plugged in and genuinely unreadable.
for d in /dev/video*; do
  if [ -e "$d" ] && [ ! -r "$d" ]; then
    echo "  note: can't read $d: sudo usermod -aG video $USER, then log out and in"; break
  fi
done

launcher camrig "camrig" "$HERE/run.sh" "$HERE/icon.png" \
  "Live preview and full v4l2 control for two cameras at once"
launcher CamView "CamView (1:1 native)" "$HERE/launch-camview.sh" "$HERE/icon.png" \
  "DECXIN live view at sensor resolution - no browser, no re-encode"

# ± opens CamView (or raises it). GNOME custom keybinding, updated in place.
if command -v gsettings >/dev/null && gsettings list-schemas 2>/dev/null | grep -qx org.gnome.settings-daemon.plugins.media-keys; then
  KB=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/camview/
  S="org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$KB"
  cur="$(gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings)"
  case "$cur" in *"$KB"*) ;; *)
    new="$(python3 -c "import ast,sys; l=ast.literal_eval(sys.argv[1].replace('@as ','')); l.append(sys.argv[2]); print(l)" "$cur" "$KB")"
    gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings "$new" ;;
  esac
  gsettings set "$S" name 'CamView (1:1 native)'
  gsettings set "$S" command "$HERE/hotkey-camview.sh"
  gsettings set "$S" binding 'plusminus'
  echo "  hotkey: ± -> CamView"
fi
