from pathlib import Path

from app.workspace import clone_tree, write_text_copy_on_write


def test_hardlinked_workspace_writes_do_not_mutate_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    original = source / "main.bean"
    original.write_text("original\n", encoding="utf-8")

    clone_tree(source, destination)
    projected = destination / "main.bean"
    assert original.stat().st_ino == projected.stat().st_ino

    write_text_copy_on_write(projected, "projected\n")

    assert original.read_text(encoding="utf-8") == "original\n"
    assert projected.read_text(encoding="utf-8") == "projected\n"
    assert original.stat().st_ino != projected.stat().st_ino
