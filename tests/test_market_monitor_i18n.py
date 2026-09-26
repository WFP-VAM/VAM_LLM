from app.services.market_monitor.i18n import (
    format_currency_value,
    format_month_label,
    format_percent_value,
    normalize_generated_text,
    resolve_report_language,
    t,
)
from app.services.market_monitor.prompt_registry import assert_prompt_manifest_valid
from app.services.market_monitor import graph as market_graph
from app.services.market_monitor.report_blocks import build_market_monitor_report_blocks


def test_report_language_auto_defaults_and_explicit_override():
    assert resolve_report_language("Democratic Republic of the Congo", "auto")["language"] == "fr"
    assert resolve_report_language("Burkina Faso", "auto")["language"] == "fr"
    assert resolve_report_language("Guatemala", "auto")["language"] == "es"
    assert resolve_report_language("Colombia", "auto")["language"] == "es"
    assert resolve_report_language("Bolivia", "auto")["language"] == "es"
    assert resolve_report_language("South Sudan", "auto")["language"] == "en"
    assert resolve_report_language("Somalia", "auto")["language"] == "en"
    assert resolve_report_language("Guatemala", "fr")["language"] == "fr"


def test_prompt_manifest_is_complete_and_synced():
    assert_prompt_manifest_valid()


def test_babel_formatting_helpers_for_supported_languages():
    assert format_currency_value(2307, "CDF", "fr") == "2 307,0 CDF"
    assert format_currency_value(2307, "CDF", "es") == "2.307,0 CDF"
    assert format_percent_value(1.5, "fr") == "1,5 %"
    assert format_percent_value(1.5, "es") == "1,5 %"
    assert format_percent_value(1.5, "en") == "1.5%"
    assert format_month_label("2026-05", "fr") == "mai 2026"
    assert format_month_label("2026-05", "es") == "mayo 2026"


def test_resolved_language_caption_uses_non_conflicting_placeholder():
    assert t("en", "ui.resolved_language", language_name="English") == "Report language: English"
    assert t("fr", "ui.resolved_language", language_name="French") == "Langue du rapport : French"
    assert t("es", "ui.resolved_language", language_name="Spanish") == "Idioma del informe: Spanish"


def test_generated_text_normalizer_preserves_protected_tokens():
    text = (
        "Price was 2307.0 CDF in 2026-06, up 12.5%. "
        "[rw_1] https://example.org/doc [INSERT GRAPH: regional_comparison] 2026 "
        "Original ReliefWeb 2026 title"
    )

    normalized, warnings = normalize_generated_text(
        text,
        "fr",
        reference_titles=["Original ReliefWeb 2026 title"],
    )

    assert warnings == []
    assert "2 307,0 CDF" in normalized
    assert "12,5 %" in normalized
    assert "2026-06" in normalized
    assert "[rw_1]" in normalized
    assert "https://example.org/doc" in normalized
    assert "[INSERT GRAPH: regional_comparison]" in normalized
    assert "Original ReliefWeb 2026 title" in normalized


def test_market_monitor_blocks_localize_headings_and_captions():
    blocks = build_market_monitor_report_blocks(
        {
            "country": "Democratic Republic of the Congo",
            "time_period": "2026-05",
            "language": "fr",
            "report_draft_sections": {
                "HIGHLIGHTS": "Texte.",
                "MARKET_OVERVIEW": "Texte.",
                "COMMODITY_ANALYSIS": "Texte.",
                "REGIONAL_HIGHLIGHTS": "Texte.",
            },
            "module_sections": {"exchange_rate": "Texte module."},
            "visualizations": {"food_basket_trend": "base64"},
        }
    )

    headings = [block.text for block in blocks if block.type == "heading"]
    captions = [block.caption for block in blocks if block.type == "figure"]

    assert "Suivi des marches - Democratic Republic of the Congo - mai 2026" in headings
    assert "Points saillants" in headings
    assert "Apercu du marche" in headings
    assert "Analyse du taux de change" in headings
    assert "Evolution du cout du panier alimentaire" in captions


def test_phase5_basket_chart_and_table_labels_are_localized():
    assert t("en", "section.BASKET_DEFINITIONS") == "Basket Definitions"
    assert t("fr", "basket.scope.selected_regions_named", regions="Nord, Sud") == (
        "Regions selectionnees : Nord, Sud"
    )
    assert t("es", "basket.table.header.composition") == "Composicion"
    assert t(
        "es",
        "chart.title.basket_trend_role",
        basket="Canasta urbana",
        scope="Nacional",
        country="Guatemala",
    ) == "Tendencia del costo de Canasta urbana - Nacional - Guatemala"


def test_deterministic_fuel_narrative_localizes_french_numbers_and_terms():
    module = market_graph.FuelEnergyModule()
    state = {
            "country": "Democratic Republic of the Congo",
            "time_period": "2026-06",
            "language": "fr",
            "fuel_energy_data": {
                "available": True,
                "unit": "CDF/Litre",
                "series": [
                    {
                        "kind": "diesel",
                        "label": "Diesel",
                        "current_price": 1120,
                        "mom_change_pct": 1.8,
                        "yoy_change_pct": 12.0,
                        "latest_month": "2026-06",
                    }
                ],
                "regional_disparities": [],
            },
        }

    narrative = module._fallback_narrative(state, state["fuel_energy_data"])
    assert "1 120,0 CDF/Litre" in narrative
    assert "1,8 %" in narrative
    assert "juin 2026" in narrative
