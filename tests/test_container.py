import os
import sys

import pytest

from email_sorter import container


def test_ids_from_puid_and_pgid():
    assert container.ids({}) == (1000, 1000)                          # the image's own user
    assert container.ids({"PUID": "1002", "PGID": " 1003 "}) == (1002, 1003)
    assert container.ids({"PUID": "1002", "PGID": ""}) == (1002, 1000)
    for bad in ({"PUID": "0"}, {"PGID": "0"}, {"PUID": "-5"}, {"PUID": "abc"}, {"PGID": "1e3"}, {"PUID": "4294967296"}):
        with pytest.raises(ValueError, match="not allowed"):
            container.ids(bad)


def test_not_owned_lists_what_belongs_to_someone_else(tmp_path):
    (tmp_path / "box" / "data").mkdir(parents=True)
    (tmp_path / "box" / "data" / "state.db").write_text("x")
    (tmp_path / "config.toml").write_text("x")
    st = tmp_path.stat()
    everything = {tmp_path, tmp_path / "box", tmp_path / "box" / "data", tmp_path / "box" / "data" / "state.db",
                  tmp_path / "config.toml"}
    assert set(container.not_owned((tmp_path,), st.st_uid + 1, st.st_gid)) == everything  # each once
    assert list(container.not_owned((tmp_path,), st.st_uid, st.st_gid)) == []
    assert list(container.not_owned((tmp_path / "missing",), 1, 1)) == []


@pytest.mark.skipif(sys.platform == "win32", reason="the container runs Linux")
def test_as_root_it_gives_the_folders_away_and_drops_root(tmp_path, monkeypatch):
    (tmp_path / "mailboxes" / "box").mkdir(parents=True)
    calls = []
    monkeypatch.setattr(container, "FOLDERS", (tmp_path / "mailboxes", tmp_path / "logs"))
    monkeypatch.setattr(container, "not_owned", lambda folders, uid, gid: iter([tmp_path / "mailboxes"]))
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "lchown", lambda p, u, g: calls.append(("chown", p, u, g)))
    for name in ("setgroups", "setgid", "setuid"):
        monkeypatch.setattr(os, name, lambda v, name=name: calls.append((name, v)))
    monkeypatch.setattr(os, "execvp", lambda f, args: calls.append(("exec", args)))
    monkeypatch.setenv("PUID", "1002")
    monkeypatch.setenv("PGID", "1003")
    container.main(["uvicorn", "app"])
    assert calls == [("chown", tmp_path / "mailboxes", 1002, 1003), ("setgroups", []), ("setgid", 1003),
                     ("setuid", 1002), ("exec", ["uvicorn", "app"])]  # the group before the user

    calls.clear()
    monkeypatch.setenv("PUID", "0")
    with pytest.raises(SystemExit, match="PUID='0' is not allowed"):
        container.main(["uvicorn"])
    assert calls == []  # nothing changed, nothing started

    monkeypatch.setattr(os, "geteuid", lambda: 1004)  # docker run --user: started as it is
    container.main(["uvicorn"])
    assert calls == [("exec", ["uvicorn"])]
