"""The Claude Code prompt in README "Adding your own devices" must match the code.

It guides people who add their own devices, so a renamed file or a profile option the
prompt does not mention makes it wrong. These checks fail when the code moves on and
the prompt is not updated with it.
"""

import inspect
import re
from pathlib import Path

from housevitals import config, history, hub, modbus, registry

ROOT = Path(__file__).resolve().parent.parent


def _prompt() -> str:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Adding your own devices", 1)[1].split("\n## ", 1)[0]
    return section.split("````text", 1)[1].split("````", 1)[0]


def test_files_named_in_the_prompt_exist():
    prompt = _prompt()
    places = [ROOT / "src" / "housevitals", ROOT / "tools", ROOT / "scripts", ROOT / "tests", ROOT]
    for name in set(re.findall(r"[\w/]+\.(?:py|md)\b", prompt)):
        if "<" in name or name.startswith("test_<"):
            continue
        if "/" in name:
            assert (ROOT / name).exists(), f"prompt names {name}, which does not exist"
        else:
            assert any((p / name).exists() for p in places), f"prompt names {name}, which does not exist"
    assert (ROOT / "src/housevitals/profiles").is_dir()


def test_every_profile_option_is_explained():
    """Every register option the profile loader reads, and every derived option, appears in
    the prompt (in backticks): a new option means the prompt needs a line about it."""
    prompt = _prompt()
    loader = inspect.getsource(registry.load_profile)
    register_options = set(re.findall(r'item(?:\.get)?\(?\[?"(\w+)"', loader)) - {"key"}
    derived_options = set(registry.DERIVED_OPTIONS) - {"key"}
    missing = sorted(o for o in register_options | derived_options | {"derived", "kind"}
                     if f"`{o}" not in prompt)
    assert not missing, f"profile options not explained in the README device prompt: {missing}"


def test_code_names_in_the_prompt_exist():
    prompt = _prompt()
    names = {"PROFILE_NAMES": registry, "DERIVED": history, "ModbusClient": modbus,
             "ModbusConnectError": modbus}
    for name, module in names.items():
        assert name in prompt and hasattr(module, name), name
    assert "energy_from_power" in prompt and "energy_from_power" in config.DeviceConfig.__dataclass_fields__
    assert "_mark_down" in inspect.getsource(hub)  # the circuit breaker the prompt describes
