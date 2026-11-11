"""Interactive, zero-flag AniKeepOrNot homelab installer.

Run with sudo from the extracted release archive or a checkout. All application runtime
preferences are stored in settings.json and secrets.json, never environment
variables or worker command-line switches.
"""

# The installed bootstrapper also runs without an application checkout.
try:
    from src.anime import APP_NAME
except ModuleNotFoundError:
    APP_NAME = __doc__.splitlines()[0].split()[2].rstrip('.')
import copy
import getpass
import grp
import json
import os
import pwd
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "legacy"))
from anime_flat_config import DEFAULT_SETTINGS, validate_config
from anime_fresh_database import initialize
from anime_admin_config_migration import migrate
from anime_flat_config import load_config

CONFIG_DIR = Path("/etc/anime-discovery")
DATA_DIR = Path("/var/lib/anime-discovery")
SERVICE = Path("/etc/systemd/system/anime-discovery.service")


def ask(label, default=None, *, secret=False):
    prompt = label + (f" [{default}]" if default else "") + ": "
    text = getpass.getpass(prompt) if secret else input(prompt).strip()
    value = text or default
    if not value:
        raise ValueError(label + " is required")
    return value


def write_json(path, data, *, mode, uid, gid):
    with path.open("x", encoding="utf-8") as output:
        json.dump(data, output, sort_keys=True, indent=2)
        output.write("\n")
    os.chmod(path, mode)
    os.chown(path, uid, gid)


def _existing_setup(account):
    """Adopt two existing configuration files without regenerating either."""
    settings_file=CONFIG_DIR/"settings.json"
    secrets_file=CONFIG_DIR/"secrets.json"
    if not settings_file.is_file() or not secrets_file.is_file():
        raise ValueError("Both existing settings.json and secrets.json are required; refusing partial configuration")
    if settings_file.is_symlink() or secrets_file.is_symlink():
        raise ValueError("Configuration symlinks are not accepted")
    if secrets_file.stat().st_mode & 0o077:
        if ask("Existing secrets.json permissions are too open; restrict to 0600? (yes/no)","no").lower()!="yes":
            raise ValueError("secrets.json must be mode 0600 before adopting configuration")
        os.chmod(secrets_file,0o600)
    print(f"Found existing {APP_NAME} configuration; keeping both files.")
    preview=migrate(CONFIG_DIR,apply=False)
    if not preview["ready"]:
        raise ValueError("Existing configuration needs review: "+
                         ", ".join(preview["unknown_settings"]+preview["unknown_secret_keys"]))
    if preview["missing_settings"]:
        print("Missing supported settings:",", ".join(preview["missing_settings"]))
        if ask("Apply missing defaults and keep an original settings backup? (yes/no)","no").lower()!="yes":
            raise ValueError("Migration declined; no changes to configuration")
        migrated=migrate(CONFIG_DIR,apply=True)
        print("Settings backup:",migrated["backup"])
    config=load_config(CONFIG_DIR)
    # Correct only known application configuration paths, with confirmation.
    # Never rewrite secrets or recursively chown unrelated directories.
    fixes=[]
    for path,mode in ((CONFIG_DIR,0o750),(settings_file,0o640),(secrets_file,0o600)):
        meta=path.stat()
        if (meta.st_uid,meta.st_gid,meta.st_mode & 0o777)!=(account.pw_uid,account.pw_gid,mode):
            fixes.append((path,mode))
    if fixes:
        print('Service account needs these ownership/mode corrections:')
        for path,mode in fixes:
            print('  ',path,'=>',account.pw_name,oct(mode))
        if ask('Apply the displayed permission corrections? (yes/no)','no').lower()!='yes':
            raise ValueError('Configuration permission corrections declined')
        for path,mode in fixes:
            os.chown(path,account.pw_uid,account.pw_gid)
            os.chmod(path,mode)
    lock=CONFIG_DIR/".settings.lock"
    if lock.exists():
        if lock.is_symlink() or not lock.is_file():
            raise ValueError("Unsafe settings lock file")
        if (lock.stat().st_uid,lock.stat().st_gid)!=(account.pw_uid,account.pw_gid):
            if ask("Fix settings lock ownership for the service user? (yes/no)","no").lower()!="yes":
                raise ValueError("Settings lock ownership requires correction")
            os.chown(lock,account.pw_uid,account.pw_gid)
        os.chmod(lock,0o600)
    return config


def _new_setup(account):
    download=Path(ask("Existing qBittorrent download directory",
                      "/mnt/media/Torrents/anime")).expanduser().resolve()
    trial=Path(ask(f"Existing {APP_NAME} staging directory",
                   "/mnt/media/Anime")).expanduser().resolve()
    if not download.is_dir() or not trial.is_dir() or download==trial:
        raise ValueError("Download and staging directories must exist and be different")
    qb_url=ask("qBittorrent API URL","https://torrents.int.lhprojects.net")
    qb_user=ask("qBittorrent username")
    qb_password=ask("qBittorrent password",secret=True)
    web_token=ask("Autobrr webhook token",secret=True)
    admin_password=ask("Dashboard admin password",secret=True)
    if web_token==admin_password:
        raise ValueError("Use different credentials for dashboard and webhooks")
    settings=copy.deepcopy(DEFAULT_SETTINGS)
    settings["storage"].update(download_root=str(download),trial_root=str(trial))
    settings["qbittorrent"].update(base_url=qb_url,username=qb_user,cleanup_source=True)
    secrets={"qbittorrent_password":qb_password,"web_token":web_token,"admin_password":admin_password}
    if ask("Enable Prowlarr? (yes/no)","no").lower()=="yes":
        settings["prowlarr"].update(enabled=True,base_url=ask("Prowlarr URL"))
        secrets["prowlarr_api_key"]=ask("Prowlarr API key",secret=True)
    if ask("Enable immediate Jellyfin library scans after staging? (yes/no)","no").lower()=="yes":
        settings["jellyfin"].update(enabled=True,base_url=ask("Jellyfin URL"))
        secrets["jellyfin_api_key"]=ask("Jellyfin API key",secret=True)
    validate_config(settings,secrets)
    CONFIG_DIR.mkdir(mode=0o750,parents=True,exist_ok=True)
    os.chmod(CONFIG_DIR,0o750)
    os.chown(CONFIG_DIR,account.pw_uid,account.pw_gid)
    write_json(CONFIG_DIR/"settings.json",settings,mode=0o640,uid=account.pw_uid,gid=account.pw_gid)
    write_json(CONFIG_DIR/"secrets.json",secrets,mode=0o600,uid=account.pw_uid,gid=account.pw_gid)
    print("Optional integrations (Sonarr, Shoko, TVDB, NTFY) default to disabled.")
    print("Configure each with: anime-discovery-admin settings configure-integration SERVICE")
    print("The installer stores all supported settings but does not invent API keys.")
    return load_config(CONFIG_DIR)


def _validate_install_settings(config):
    settings,secrets=config["settings"],config["secrets"]
    storage=settings["storage"]
    download=Path(storage["download_root"]).expanduser()
    trial=Path(storage["trial_root"]).expanduser()
    if not download.is_dir() or not trial.is_dir() or download.resolve()==trial.resolve():
        raise ValueError("Configured storage paths must exist and be separate directories")
    if not settings["qbittorrent"]["username"] or not secrets.get("qbittorrent_password"):
        raise ValueError("Existing qBittorrent username/password missing")
    if not secrets.get("web_token"):
        raise ValueError("Webhook token missing")
    if not secrets.get("admin_password"):
        raise ValueError("Dashboard password missing; configure credentials before retrying")
    db_path=Path(settings["daemon"]["database_path"])
    if not db_path.is_absolute():
        raise ValueError("Database path must be absolute")
    if db_path.exists() or db_path.is_symlink():
        raise FileExistsError("A database already exists at "+str(db_path)+
                             "; remove/archive the abandoned database yourself before first install")
    if any(Path(str(db_path)+suffix).exists() for suffix in ("-wal","-shm","-journal")):
        raise ValueError("Old SQLite sidecars exist at "+str(db_path))
    return db_path


def main():
    if os.geteuid()!=0:
        raise SystemExit("Run with sudo: sudo python3 deploy/install-homelab.py")
    if SERVICE.exists() or SERVICE.is_symlink():
        raise SystemExit("Existing service detected; use the admin upgrade command")
    if not shutil.which("systemctl"):
        raise SystemExit("systemd is required for automatic service installation")
    settings_file=CONFIG_DIR/"settings.json"
    secrets_file=CONFIG_DIR/"secrets.json"
    existing=settings_file.exists() or secrets_file.exists()
    if existing and not (settings_file.is_file() and secrets_file.is_file()):
        raise SystemExit("Partial existing configuration detected; both JSON files are required")
    username=ask("Existing Linux service account", "anime-discovery")
    account=pwd.getpwnam(username)
    if account.pw_uid==0:
        raise ValueError("Service account must be non-root")
    media=grp.getgrnam("media")
    if media.gr_gid!=1002:
        raise ValueError("Expected media GID 1002; found "+str(media.gr_gid)+". Check the VM group configuration")
    if username not in media.gr_mem and account.pw_gid!=media.gr_gid:
        raise ValueError("Service account must belong to media (GID 1002) before installation")
    print(f"Installing {APP_NAME} for account:",username,"and media GID:",media.gr_gid)
    if not os.access(ROOT,os.X_OK):
        raise ValueError("Application directory not accessible")
    config=_existing_setup(account) if existing else _new_setup(account)
    db_path=_validate_install_settings(config)
    db_path.parent.mkdir(mode=0o750,parents=True,exist_ok=True)
    # Do not change ownership of existing directories; ensure the service
    # account can use the intended storage and database locations.
    if not os.access(db_path.parent,os.W_OK):
        raise ValueError("Database parent directory is not writable")
    if db_path.parent==DATA_DIR and db_path.parent.stat().st_uid!=account.pw_uid:
        os.chown(db_path.parent,account.pw_uid,account.pw_gid)
    if db_path.parent.stat().st_uid!=account.pw_uid and not (db_path.parent.stat().st_mode & 0o002):
        raise ValueError("Database directory must be writable by the service account")
    initialize(db_path)
    os.chown(db_path,account.pw_uid,account.pw_gid)

    media_group="SupplementaryGroups=media\n"
    service=f"""[Unit]
Description={APP_NAME} dashboard, Autobrr intake and acquisition workers
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={{user}}
Group={{group}}
{{media_group}}WorkingDirectory={{root}}
ExecStart={{python}} {{root}}/src/legacy/anime-discovery-live.py
Restart=on-failure
RestartSec=5
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
""".format(user=username,group=grp.getgrgid(account.pw_gid).gr_name,
           root=ROOT,python=sys.executable,media_group=media_group)
    SERVICE.write_text(service,encoding="utf-8")
    subprocess.run(["systemctl","daemon-reload"],check=True)
    subprocess.run(["systemctl","enable","--now","anime-discovery.service"],check=True)
    print(f"{APP_NAME} service started; open http://SERVER-IP:"+
          str(config["settings"]["web"]["port"])+"/")
    print("Dashboard username: anime ; use your separate admin password")
    print("Use: systemctl status anime-discovery.service")


if __name__ == "__main__":
    main()
