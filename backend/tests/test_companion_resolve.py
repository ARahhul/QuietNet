import shutil

from pahra.assistant.mcp_client import resolve_companion_command


def _venv_with_python(tmp_path):
    for rel in ("Scripts/python.exe", "bin/python"):
        p = tmp_path / ".venv" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("")
    return tmp_path


def test_uses_venv_python_module_when_not_on_path(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    target = resolve_companion_command("pahra-companion --log-level DEBUG", companion_dir=_venv_with_python(tmp_path))
    assert target is not None
    assert target.command.endswith(("python.exe", "python"))
    assert target.args == ["-m", "pahra_companion", "--log-level", "DEBUG"]
    assert target.cwd == str(tmp_path)


def test_prefers_the_command_on_path(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "C:/tools/pahra-companion.exe")
    target = resolve_companion_command("pahra-companion", companion_dir=_venv_with_python(tmp_path))
    assert target.command == "C:/tools/pahra-companion.exe" and target.args == []


def test_missing_everywhere_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert resolve_companion_command("pahra-companion", companion_dir=tmp_path) is None
    assert resolve_companion_command("", companion_dir=tmp_path) is None
    assert resolve_companion_command("something-else", companion_dir=_venv_with_python(tmp_path)) is None
