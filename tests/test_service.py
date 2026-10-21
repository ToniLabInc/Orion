"""service.render_ollama_unit: OLLAMA_HOST without its scheme, flash attention as 1/0, the models folder quoted, and
no keep-alive (it goes with every request)."""

from pathlib import Path

import pytest

from orion.service import OllamaUnit, render_ollama_unit


@pytest.mark.parametrize("flash", [True, False])
def test_ollama_unit(tmp_path: Path, flash: bool) -> None:
    """The unit carries the settings as Ollama reads them, a data folder with spaces kept as one value."""
    text = render_ollama_unit(OllamaUnit("http://127.0.0.1:11434", tmp_path / "my data", flash, "q8_0", 1, 1))
    assert "Environment=OLLAMA_HOST=127.0.0.1:11434\n" in text
    assert f"Environment=OLLAMA_FLASH_ATTENTION={flash:d}\n" in text
    assert f'Environment="OLLAMA_MODELS={tmp_path}/my data"\n' in text
    assert "KEEP_ALIVE" not in text
    with pytest.raises(ValueError, match="absolute"):
        OllamaUnit("http://127.0.0.1:11434", Path("data/models/ollama"), flash, "q8_0", 1, 1)


def test_vitals_programs() -> None:
    """The busiest programs: at most the asked number, each with a name, a share of the whole CPU and its memory, the
    readings repeated with every snapshot until they are read again; or the heaviest, by memory."""
    from orion.service import Vitals

    vitals = Vitals("/")
    top = vitals.programs(3, by="cpu")
    assert 0 < len(top) <= 3
    for program in top:
        assert program["name"] and 0 <= program["cpu"] <= 100 and program["mem"] >= 0
    assert vitals.snapshot(slow=False)["top"] == top  # not read again
    heavy = vitals.programs(5, by="memory")
    assert [p["mem"] for p in heavy] == sorted((p["mem"] for p in heavy), reverse=True)
