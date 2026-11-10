"""The self-updater: installs a good download, refuses a broken one, and never touches settings."""
import io
import tarfile
from pathlib import Path

from penultimate.updater import Updater

HERE = Path(__file__).resolve().parent.parent


def make_tar(tmp_path, break_engine=False, marker="NEW"):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for f in sorted((HERE / "penultimate").rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts:
                data = f.read_bytes()
                if f.name == "engine.py" and break_engine:
                    data = b"def broken(:\n"
                if f.name == "regime.py":
                    data += f"\nMARKER = '{marker}'\n".encode()
                info = tarfile.TarInfo("Owner-repo-abc1234/" + str(f.relative_to(HERE)))
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        for name, data in (("requirements.txt", (HERE / "requirements.txt").read_bytes()),
                           ("config.yaml", b"mode: live\n"), ("limits.yaml", b"max_order_usd: 999999\n")):
            info = tarfile.TarInfo("Owner-repo-abc1234/" + name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def install(tmp_path, tar_bytes, mode="paper", auto="practice"):
    root, data = tmp_path / "root", tmp_path / "root" / "data"
    data.mkdir(parents=True)
    (root / "penultimate").mkdir()
    (root / "penultimate" / "regime.py").write_text("OLD = 1\n")
    (root / "config.yaml").write_text("mode: paper\n")
    (root / "limits.yaml").write_text("max_order_usd: 100\n")
    (root / "requirements.txt").write_bytes((HERE / "requirements.txt").read_bytes())
    (data / "installed.sha").write_text("old0000")
    u = Updater({"mode": mode, "update": {"repo": "Owner/repo", "auto": auto}}, root, data)
    u.fetch_latest = lambda: ("new1111", "a better version")
    u.fetch_tar = lambda sha: tar_bytes
    return u, root, data


def test_a_good_version_is_installed_and_settings_are_left_alone(tmp_path):
    u, root, data = install(tmp_path, make_tar(tmp_path))
    u.check()
    assert u.available and u.view()["message"] == "a better version"
    u.apply(u.latest)
    assert "MARKER = 'NEW'" in (root / "penultimate" / "regime.py").read_text()
    assert (root / "config.yaml").read_text() == "mode: paper\n"            # never replaced
    assert (root / "limits.yaml").read_text() == "max_order_usd: 100\n"     # never replaced
    assert (data / "installed.sha").read_text() == "new1111" and (data / "restart.flag").exists()
    assert not u.available and not list(data.glob("update-*"))


def test_a_version_that_does_not_compile_is_refused(tmp_path):
    u, root, data = install(tmp_path, make_tar(tmp_path, break_engine=True))
    u.check()
    u.apply(u.latest)
    assert u.state == "error" and "not installed" in u.error
    assert (root / "penultimate" / "regime.py").read_text() == "OLD = 1\n"  # running files untouched
    assert (data / "installed.sha").read_text() == "old0000" and not (data / "restart.flag").exists()


def test_a_download_with_an_unsafe_path_is_refused(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("../../evil.py")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    u, root, data = install(tmp_path, buf.getvalue())
    u.check()
    u.apply(u.latest)
    assert u.state == "error" and not (tmp_path / "evil.py").exists()


def test_with_real_money_it_waits_for_a_click(tmp_path):
    u, root, data = install(tmp_path, make_tar(tmp_path), mode="live")
    calls = []
    u.apply = lambda sha: calls.append(sha)
    u.one_pass()
    assert calls == []                       # live: nothing installs on its own
    u.request(install=True)
    u.one_pass()
    assert calls == ["new1111"]              # after the click it does


def test_it_is_off_without_an_update_section(tmp_path):
    assert not Updater({"mode": "paper"}, tmp_path, tmp_path).enabled
