"""Select typing speeds for local and remote applications."""
import copy
import ntpath


def select_speed_profile(settings, executable, mode):
    """Only Remote Desktop has an override; all other applications use global speeds."""
    name = ntpath.basename(executable or "").casefold()
    remote = settings["remote_desktop"]
    citrix = (remote["enabled"] and remote["citrix_enabled"]
              and name in {item.casefold() for item in remote["citrix_executables"]})
    matched = citrix or (remote["enabled"] and name in {item.casefold() for item in remote["executables"]})
    profiles = remote["profiles"] if matched else settings["profiles"]
    return copy.deepcopy(profiles[mode]), "Citrix Workspace" if citrix else "远程桌面" if matched else "通用"
