"""Contract tests for the integration metadata."""

from __future__ import annotations

import json
import re
from pathlib import Path

COMPONENT_DIR = Path(__file__).parents[1] / "custom_components" / "thermal_model"
MANIFEST = json.loads((COMPONENT_DIR / "manifest.json").read_text("utf-8"))
STRINGS = json.loads((COMPONENT_DIR / "strings.json").read_text("utf-8"))
TRANSLATIONS = {
    lang: json.loads((COMPONENT_DIR / "translations" / f"{lang}.json").read_text("utf-8"))
    for lang in ("en", "fr")
}


def test_domain_matches_folder_name() -> None:
    """hassfest requires the domain to be the component folder name."""
    assert MANIFEST["domain"] == COMPONENT_DIR.name


def test_required_manifest_keys() -> None:
    """Keys that hassfest and HACS both need."""
    for key in (
        "domain",
        "name",
        "codeowners",
        "config_flow",
        "documentation",
        "iot_class",
        "issue_tracker",
        "requirements",
        "version",
    ):
        assert key in MANIFEST, key
    assert MANIFEST["config_flow"] is True


def test_zero_external_dependencies() -> None:
    """Standing decision: no numpy/scipy, hence no requirements at all."""
    assert MANIFEST["requirements"] == []


def test_version_is_semver() -> None:
    """Version must be a plain semantic version for HACS releases."""
    assert re.fullmatch(r"\d+\.\d+\.\d+", MANIFEST["version"])


def test_recorder_is_an_after_dependency() -> None:
    """We read the recorder but must not block HA start when it is absent."""
    assert "recorder" in MANIFEST["after_dependencies"]


def test_dependencies_are_only_what_the_panel_needs() -> None:
    """`http` and `panel_custom` only: the panel is the one hard requirement.

    The recorder stays an *after* dependency even though the panel plots its
    history: making it a hard dependency would refuse to start the integration on
    an installation without the recorder, for a chart the panel can degrade on.
    """
    assert sorted(MANIFEST["dependencies"]) == ["http", "panel_custom"]


def test_translations_cover_every_step() -> None:
    """Every config/options step needs a translation, or the UI shows raw keys."""
    reference_steps = set(STRINGS["config"]["step"])
    for lang, data in TRANSLATIONS.items():
        assert set(data["config"]["step"]) == reference_steps, lang
        assert set(data["config"]["error"]) == set(STRINGS["config"]["error"]), lang
        assert set(data["options"]["step"]) == set(STRINGS["options"]["step"]), lang


def test_translation_data_keys_match_schema() -> None:
    """A translated field that is not in the schema is a silent UI bug."""
    for lang, data in TRANSLATIONS.items():
        for step_id, step in data["config"]["step"].items():
            reference = STRINGS["config"]["step"][step_id]
            if "data" in reference:
                assert set(step.get("data", {})) == set(reference["data"]), (lang, step_id)
