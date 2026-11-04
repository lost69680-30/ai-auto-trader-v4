from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_storage_guardian_runtime_source_is_mirrored_for_render_assembly():
    source = ROOT / "app/engines/storage_guardian.py"
    packaged = ROOT / "extracted-source/ai-auto-trader-v4-assembly/app/engines/storage_guardian.py"
    assert source.is_file()
    assert packaged.is_file()
    assert source.read_bytes() == packaged.read_bytes()
