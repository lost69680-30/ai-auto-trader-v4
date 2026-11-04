from pathlib import Path
import xml.etree.ElementTree as ET


ROOT = Path(__file__).parents[1]
ANDROID_NS = "{http://schemas.android.com/apk/res/android}"
ROOT_ANDROID = ROOT / "android/app/src/main"
PACKAGED_ANDROID = ROOT / "extracted-source/ai-auto-trader-v4-assembly/android/app/src/main"

BRAND_RESOURCES = (
    "AndroidManifest.xml",
    "java/com/aiautotrader/v4/MainActivity.kt",
    "res/values/colors.xml",
    "res/values/styles.xml",
    "res/drawable/ic_solo_logo.xml",
    "res/drawable/ic_solo_logo_light.xml",
    "res/drawable/ic_solo_emblem_foreground.xml",
    "res/drawable/ic_solo_emblem_monochrome.xml",
    "res/drawable/ic_solo_emblem_light.xml",
    "res/drawable/ic_solo_notification.xml",
    "res/mipmap-anydpi-v26/ic_launcher.xml",
    "res/mipmap-anydpi-v26/ic_launcher_round.xml",
)


def _xml(path: Path):
    return ET.parse(path).getroot()


def _path_data(path: Path) -> list[str]:
    return [
        element.attrib[f"{ANDROID_NS}pathData"]
        for element in _xml(path).findall("path")
        if f"{ANDROID_NS}pathData" in element.attrib
    ]


def test_manifest_uses_android_adaptive_launcher_icons():
    manifest = _xml(ROOT_ANDROID / "AndroidManifest.xml")
    application = manifest.find("application")
    assert application is not None
    assert application.attrib[f"{ANDROID_NS}icon"] == "@mipmap/ic_launcher"
    assert application.attrib[f"{ANDROID_NS}roundIcon"] == "@mipmap/ic_launcher_round"


def test_adaptive_icons_use_the_hidden_sovereign_layers():
    expected = {
        "background": "@color/solo_obsidian",
        "foreground": "@drawable/ic_solo_emblem_foreground",
        "monochrome": "@drawable/ic_solo_emblem_monochrome",
    }
    for name in ("ic_launcher.xml", "ic_launcher_round.xml"):
        icon = _xml(ROOT_ANDROID / "res/mipmap-anydpi-v26" / name)
        assert icon.tag == "adaptive-icon"
        layers = {
            child.tag: child.attrib[f"{ANDROID_NS}drawable"]
            for child in icon
        }
        assert layers == expected


def test_crowned_lion_s_geometry_is_identical_across_brand_variants():
    foreground = _path_data(ROOT_ANDROID / "res/drawable/ic_solo_emblem_foreground.xml")
    assert len(foreground) >= 4
    variants = (
        "ic_solo_emblem_monochrome.xml",
        "ic_solo_emblem_light.xml",
        "ic_solo_notification.xml",
    )
    for name in variants:
        assert _path_data(ROOT_ANDROID / "res/drawable" / name) == foreground

    for name in ("ic_solo_logo.xml", "ic_solo_logo_light.xml"):
        assert _path_data(ROOT_ANDROID / "res/drawable" / name)[1:] == foreground


def test_brand_palette_uses_the_approved_hidden_sovereign_tokens():
    root = _xml(ROOT_ANDROID / "res/values/colors.xml")
    colors = {element.attrib["name"]: (element.text or "").strip().upper() for element in root.findall("color")}
    assert colors == {
        "solo_obsidian": "#101216",
        "solo_graphite": "#1B1E24",
        "solo_champagne_gold": "#C6A66B",
        "solo_antique_gold": "#927547",
        "solo_platinum": "#D7D9DC",
        "solo_silk_ivory": "#F4F0E8",
        "solo_midnight_bronze": "#403426",
        "solo_success": "#39B980",
        "solo_warning": "#E7B65E",
        "solo_danger": "#E36F72",
    }


def test_root_and_render_assembly_brand_resources_are_byte_identical():
    for relative in BRAND_RESOURCES:
        assert (ROOT_ANDROID / relative).read_bytes() == (PACKAGED_ANDROID / relative).read_bytes()


def test_android_header_selects_the_light_emblem_for_light_theme():
    main_activity = (ROOT_ANDROID / "java/com/aiautotrader/v4/MainActivity.kt").read_text(encoding="utf-8")
    assert "painterResource(id = if (lightTheme) R.drawable.ic_solo_logo_light else R.drawable.ic_solo_logo)" in main_activity
