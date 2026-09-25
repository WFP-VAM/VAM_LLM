"""Localization helpers for Market Monitor reports."""
from __future__ import annotations

import logging
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd
from babel.dates import format_date
from babel.numbers import format_currency, format_decimal

from app.shared.countries import resolve_country

logger = logging.getLogger(__name__)

SUPPORTED_REPORT_LANGUAGES = {"en", "fr", "es"}
LANGUAGE_LOCALES = {"en": "en_US", "fr": "fr_FR", "es": "es_ES"}
LANGUAGE_NAMES = {"en": "English", "fr": "French", "es": "Spanish"}

FRENCH_DEFAULT_ISO3 = {
    "BDI",
    "BFA",
    "CAF",
    "CMR",
    "COD",
    "COG",
    "CIV",
    "DJI",
    "GIN",
    "GNB",
    "HTI",
    "MDG",
    "MLI",
    "MRT",
    "NER",
    "SEN",
    "TCD",
}

SPANISH_DEFAULT_ISO3 = {"BOL", "COL", "ECU", "GTM", "HND", "SLV", "VEN"}


STRINGS: Dict[str, Dict[str, str]] = {
    "en": {
        "report.title": "Market Monitor",
        "section.HIGHLIGHTS": "Highlights",
        "section.BASKET_DEFINITIONS": "Basket Definitions",
        "section.MARKET_OVERVIEW": "Market Overview",
        "section.COMMODITY_ANALYSIS": "Commodity Analysis",
        "section.REGIONAL_HIGHLIGHTS": "Regional Highlights",
        "section.REFERENCES": "References",
        "module.exchange_rate": "Exchange Rate Analysis",
        "module.fuel_energy": "Fuel & Energy",
        "module.livestock_animal_products": "Livestock & Animal Products",
        "module.labour_market": "Labour Market",
        "figure.food_basket_trend": "Food basket cost trend",
        "figure.basket_trend_role": "{basket} cost trend ({scope})",
        "figure.basket_regional_role": "{basket} regional cost ({scope}, {period})",
        "figure.fuel_prices": "Fuel retail price trend",
        "figure.livestock_animal_products": "Livestock and animal product price trend",
        "figure.labour_market": "Labour market trend",
        "chart.label.current": "Current",
        "chart.label.official": "Official",
        "chart.label.unofficial": "Unofficial",
        "chart.axis.cost": "Cost",
        "chart.axis.price": "Price",
        "chart.axis.wage": "Wage",
        "chart.title.food_basket": "Food Basket Cost Trend - {country}",
        "chart.title.basket_trend_role": "{basket} Cost Trend - {scope} - {country}",
        "chart.title.basket_regional_role": "{basket} Regional Cost - {scope} - {period}",
        "chart.title.commodity": "Commodity Price Trends - {country} - {title_suffix}",
        "chart.title.exchange_rate": "Exchange Rate Trend - {country}",
        "chart.title.fuel": "Fuel Prices - {country}",
        "chart.title.livestock": "Livestock & Animal Products - {country}",
        "chart.title.labour": "Labour Market - {country}",
        "chart.title.regional": "Regional Food Basket Cost - {period}",
        "chart.axis.currency": "{label} ({currency})",
        "chart.axis.fx": "{currency} per 1 USD",
        "chart.axis.fuel": "{currency}/Litre",
        "chart.axis.unit": "{currency}/{unit}",
        "chart.axis.day": "{currency}/day",
        "chart.axis.index_first_month": "Index (first month = 100)",
        "chart.axis.kg_per_wage": "kg of {staple} per day's wage",
        "chart.page_suffix": "{category} (Page {page_idx}/{page_count})",
        "basket.role.primary": "Primary",
        "basket.role.secondary": "Secondary",
        "basket.scope.national": "National",
        "basket.scope.selected_regions": "Selected regions",
        "basket.scope.selected_regions_named": "Selected regions: {regions}",
        "basket.table.header.basket": "Basket / role",
        "basket.table.header.description": "Description",
        "basket.table.header.scope": "Scope",
        "basket.table.header.composition": "Composition",
        "commodity_category.cereals": "Cereals",
        "commodity_category.pulses": "Pulses",
        "commodity_category.oil": "Oil",
        "commodity_category.sugar": "Sugar",
        "commodity_category.condiments": "Condiments",
        "commodity_category.vegetables": "Vegetables",
        "commodity_category.livestock": "Livestock",
        "commodity_category.other": "Other",
        "ui.language": "Language",
        "ui.language.auto": "Auto",
        "ui.language.en": "English",
        "ui.language.fr": "Francais",
        "ui.language.es": "Espanol",
        "ui.resolved_language": "Report language: {language_name}",
        "ui.run_id": "Run ID",
        "ui.country": "Country",
        "ui.time_period": "Time Period",
        "ui.llm_calls": "LLM Calls",
        "ui.warnings": "Warnings",
        "ui.qa_review": "QA review",
        "ui.qa_status": "Status: {status}; correction attempts: {attempts}",
        "ui.qa_flag": "[{severity}] {section}: {details}",
        "ui.cache_metadata": "Cache metadata",
        "ui.report_sections": "Report Sections",
        "ui.visualizations": "Visualizations",
        "ui.data_statistics": "Data Statistics",
        "ui.export_unavailable": "Export is available for asynchronous runs only.",
        "ui.preparing_docx": "Preparing DOCX...",
        "ui.download_docx": "Generate & Download DOCX",
        "ui.report_ready": "Report ready",
        "ui.report_ready_caption": "Your report has been generated. Download the Word file or preview it on this page.",
        "ui.download_report": "Download report (.docx)",
        "ui.view_report": "View report on this page",
        "ui.hide_report": "Hide report preview",
        "ui.technical_details": "Technical details",
        "ui.export_error": "The Word file could not be prepared. You can still preview the report below.",
        "ui.retry_export": "Retry preparing download",
        "live.price_data.title": "Price Data",
        "live.price_data.summary": "{rows} price row(s) retrieved for {country} ({period}) from cache plus any targeted backfill.",
        "live.price_data.mock": "Mock data is enabled for this run, so no price rows were read.",
        "live.seerist.title": "Seerist Documents",
        "live.reliefweb.title": "ReliefWeb Documents",
        "live.docs.summary": "{count} {source} documents retrieved.",
        "live.docs.unavailable": "{source} retrieval unavailable: {error}",
        "warning.partial_basket": "Food basket for {period} is based on {latest_count} of {selected_count} selected commodities with target-month data ({latest_names}). Missing target-month components: {missing_names}.",
        "warning.exchange_stable_driver": "Exchange rate classification is 'stable' but trend_analysis.key_market_drivers references currency depreciation. Avoid asserting current depreciation; if mentioned, frame as context/discrepancy.",
        "warning.skip_exchange_usd": "Skipped exchange_rate module because currency_code is USD (no exchange-rate pair to fetch).",
        "warning.skip_fuel_missing": "Skipped fuel_energy module because no transport fuel price data was available.",
        "warning.skip_livestock_missing": "Skipped livestock_animal_products module because no livestock or animal product price data was available.",
        "warning.skip_labour_missing": "Skipped labour_market module because no wage price data was available.",
        "warning.skip_exchange_required": "Skipped exchange_rate module due to missing required inputs: {missing}",
        "warning.skip_exchange_source": "Skipped exchange_rate module because no exchange-rate source was available: {error}",
        "warning.skip_fuel_error": "Skipped fuel_energy module because no transport fuel price data was available: {error}",
        "warning.skip_livestock_error": "Skipped livestock_animal_products module because no livestock or animal product price data was available: {error}",
        "warning.skip_labour_error": "Skipped labour_market module because no wage price data was available: {error}",
        "warning.qa_unresolved": "Automated QA completed with {count} unresolved high/medium issue(s). Review the structured QA findings before publication.",
        "fallback.exchange": "The {currency} is at {rate} per USD.",
        "fallback.fuel.driver": "The movement is consistent with fuel-market, logistics, or administered price conditions.",
        "fallback.fuel.implication": "Fuel prices affect transport and distribution costs, feeding into staple and food-basket prices and shaping household purchasing power.",
        "fallback.fuel.regional": " Regional differences were notable, with {highest} above {lowest} for {label}.",
        "fallback.fuel.metric": "{label} averaged {current} {unit} in {month}, {mom} month-on-month{yoy}",
        "fallback.yoy": " and {value} year-on-year",
        "fallback.livestock.driver": "The movement is consistent with seasonal demand, supply, feed, pasture, or water conditions.",
        "fallback.livestock.implication": "These prices shape animal-source protein affordability and dietary diversity for households.",
        "fallback.livestock.live_animal": " Live-animal prices also affect pastoralist income and livestock-to-cereal terms of trade.",
        "fallback.livestock.regional": " Regional differences were notable, with {highest} above {lowest} for {label}.",
        "fallback.livestock.metric": "{label} averaged {current} {unit} in {month}, {mom} month-on-month{yoy}",
        "fallback.labour.metric": "{label} averaged {current} {unit} in {month}, {mom} month-on-month{yoy}.",
        "fallback.labour.skilled": " {label} averaged {current} {unit}.",
        "fallback.labour.availability": " Labour availability was {availability}.",
        "fallback.labour.purchasing_power": " One day's wage bought about {kg} kg of {staple} in {month}, {mom} month-on-month.",
        "fallback.labour.driver": "The movement is consistent with seasonal labour demand, harvest-cycle effects, or broader economic conditions.",
    },
    "fr": {
        "report.title": "Suivi des marches",
        "section.HIGHLIGHTS": "Points saillants",
        "section.BASKET_DEFINITIONS": "Definitions des paniers",
        "section.MARKET_OVERVIEW": "Apercu du marche",
        "section.COMMODITY_ANALYSIS": "Analyse des produits",
        "section.REGIONAL_HIGHLIGHTS": "Points saillants regionaux",
        "section.REFERENCES": "References",
        "module.exchange_rate": "Analyse du taux de change",
        "module.fuel_energy": "Carburants et energie",
        "module.livestock_animal_products": "Betail et produits animaux",
        "module.labour_market": "Marche du travail",
        "figure.food_basket_trend": "Evolution du cout du panier alimentaire",
        "figure.basket_trend_role": "Evolution du cout de {basket} ({scope})",
        "figure.basket_regional_role": "Cout regional de {basket} ({scope}, {period})",
        "figure.fuel_prices": "Evolution des prix de detail des carburants",
        "figure.livestock_animal_products": "Evolution des prix du betail et des produits animaux",
        "figure.labour_market": "Evolution du marche du travail",
        "chart.label.current": "Actuel",
        "chart.label.official": "Officiel",
        "chart.label.unofficial": "Parallele",
        "chart.axis.cost": "Cout",
        "chart.axis.price": "Prix",
        "chart.axis.wage": "Salaire",
        "chart.title.food_basket": "Evolution du cout du panier alimentaire - {country}",
        "chart.title.basket_trend_role": "Evolution du cout de {basket} - {scope} - {country}",
        "chart.title.basket_regional_role": "Cout regional de {basket} - {scope} - {period}",
        "chart.title.commodity": "Evolution des prix des produits - {country} - {title_suffix}",
        "chart.title.exchange_rate": "Evolution du taux de change - {country}",
        "chart.title.fuel": "Prix des carburants - {country}",
        "chart.title.livestock": "Betail et produits animaux - {country}",
        "chart.title.labour": "Marche du travail - {country}",
        "chart.title.regional": "Cout regional du panier alimentaire - {period}",
        "chart.axis.currency": "{label} ({currency})",
        "chart.axis.fx": "{currency} pour 1 USD",
        "chart.axis.fuel": "{currency}/litre",
        "chart.axis.unit": "{currency}/{unit}",
        "chart.axis.day": "{currency}/jour",
        "chart.axis.index_first_month": "Indice (premier mois = 100)",
        "chart.axis.kg_per_wage": "kg de {staple} par jour de salaire",
        "chart.page_suffix": "{category} (page {page_idx}/{page_count})",
        "basket.role.primary": "Principal",
        "basket.role.secondary": "Secondaire",
        "basket.scope.national": "National",
        "basket.scope.selected_regions": "Regions selectionnees",
        "basket.scope.selected_regions_named": "Regions selectionnees : {regions}",
        "basket.table.header.basket": "Panier / role",
        "basket.table.header.description": "Description",
        "basket.table.header.scope": "Portee",
        "basket.table.header.composition": "Composition",
        "commodity_category.cereals": "Cereales",
        "commodity_category.pulses": "Legumineuses",
        "commodity_category.oil": "Huile",
        "commodity_category.sugar": "Sucre",
        "commodity_category.condiments": "Condiments",
        "commodity_category.vegetables": "Legumes",
        "commodity_category.livestock": "Betail",
        "commodity_category.other": "Autres",
        "ui.language": "Langue",
        "ui.language.auto": "Auto",
        "ui.language.en": "English",
        "ui.language.fr": "Francais",
        "ui.language.es": "Espanol",
        "ui.resolved_language": "Langue du rapport : {language_name}",
        "ui.run_id": "ID d'execution",
        "ui.country": "Pays",
        "ui.time_period": "Periode",
        "ui.llm_calls": "Appels LLM",
        "ui.warnings": "Avertissements",
        "ui.qa_review": "Controle qualite",
        "ui.qa_status": "Statut : {status} ; tentatives de correction : {attempts}",
        "ui.qa_flag": "[{severity}] {section} : {details}",
        "ui.cache_metadata": "Metadonnees du cache",
        "ui.report_sections": "Sections du rapport",
        "ui.visualizations": "Visualisations",
        "ui.data_statistics": "Statistiques des donnees",
        "ui.export_unavailable": "L'export est disponible uniquement pour les executions asynchrones.",
        "ui.preparing_docx": "Preparation du DOCX...",
        "ui.download_docx": "Generer et telecharger le DOCX",
        "ui.report_ready": "Rapport pret",
        "ui.report_ready_caption": "Votre rapport a ete genere. Telechargez le fichier Word ou affichez-le sur cette page.",
        "ui.download_report": "Telecharger le rapport (.docx)",
        "ui.view_report": "Afficher le rapport sur cette page",
        "ui.hide_report": "Masquer l'apercu du rapport",
        "ui.technical_details": "Details techniques",
        "ui.export_error": "Le fichier Word n'a pas pu etre prepare. Vous pouvez toujours afficher le rapport ci-dessous.",
        "ui.retry_export": "Reessayer de preparer le telechargement",
        "live.price_data.title": "Donnees de prix",
        "live.price_data.summary": "{rows} ligne(s) de prix recuperee(s) pour {country} ({period}) depuis le cache et les retours cibles.",
        "live.price_data.mock": "Les donnees factices sont activees pour cette execution; aucune ligne de prix n'a ete lue.",
        "live.seerist.title": "Documents Seerist",
        "live.reliefweb.title": "Documents ReliefWeb",
        "live.docs.summary": "{count} documents {source} recuperes.",
        "live.docs.unavailable": "Recuperation {source} indisponible : {error}",
        "warning.partial_basket": "Le panier alimentaire pour {period} repose sur {latest_count} des {selected_count} produits selectionnes avec des donnees du mois cible ({latest_names}). Composants manquants pour le mois cible : {missing_names}.",
        "warning.exchange_stable_driver": "La classification du taux de change est 'stable', mais trend_analysis.key_market_drivers mentionne une depreciation de la monnaie. Eviter d'affirmer une depreciation actuelle; si elle est mentionnee, la presenter comme contexte ou divergence.",
        "warning.skip_exchange_usd": "Module exchange_rate ignore car currency_code est USD (aucune paire de change a recuperer).",
        "warning.skip_fuel_missing": "Module fuel_energy ignore car aucune donnee de prix des carburants de transport n'etait disponible.",
        "warning.skip_livestock_missing": "Module livestock_animal_products ignore car aucune donnee de prix du betail ou des produits animaux n'etait disponible.",
        "warning.skip_labour_missing": "Module labour_market ignore car aucune donnee de salaire n'etait disponible.",
        "warning.skip_exchange_required": "Module exchange_rate ignore en raison d'entrees obligatoires manquantes : {missing}",
        "warning.skip_exchange_source": "Module exchange_rate ignore car aucune source de taux de change n'etait disponible : {error}",
        "warning.skip_fuel_error": "Module fuel_energy ignore car aucune donnee de prix des carburants de transport n'etait disponible : {error}",
        "warning.skip_livestock_error": "Module livestock_animal_products ignore car aucune donnee de prix du betail ou des produits animaux n'etait disponible : {error}",
        "warning.skip_labour_error": "Module labour_market ignore car aucune donnee de salaire n'etait disponible : {error}",
        "warning.qa_unresolved": "Le controle qualite automatise s'est termine avec {count} probleme(s) eleve(s)/moyen(s) non resolu(s). Examinez les resultats structures avant publication.",
        "fallback.exchange": "Le {currency} s'etablit a {rate} par USD.",
        "fallback.fuel.driver": "L'evolution est coherente avec les conditions du marche des carburants, de la logistique ou des prix administres.",
        "fallback.fuel.implication": "Les prix des carburants influencent les couts de transport et de distribution, avec des effets sur les prix des denrees de base, le panier alimentaire et le pouvoir d'achat des menages.",
        "fallback.fuel.regional": " Les ecarts regionaux etaient notables, avec {highest} au-dessus de {lowest} pour {label}.",
        "fallback.fuel.metric": "{label} s'est etabli a {current} {unit} en {month}, soit {mom} en glissement mensuel{yoy}",
        "fallback.yoy": " et {value} en glissement annuel",
        "fallback.livestock.driver": "L'evolution est coherente avec la demande saisonniere, l'offre, les aliments pour animaux, les paturages ou les conditions hydriques.",
        "fallback.livestock.implication": "Ces prix influencent l'accessibilite economique des proteines animales et la diversite alimentaire des menages.",
        "fallback.livestock.live_animal": " Les prix du betail vivant influencent aussi les revenus pastoraux et les termes de l'echange betail/cereales.",
        "fallback.livestock.regional": " Les ecarts regionaux etaient notables, avec {highest} au-dessus de {lowest} pour {label}.",
        "fallback.livestock.metric": "{label} s'est etabli a {current} {unit} en {month}, soit {mom} en glissement mensuel{yoy}",
        "fallback.labour.metric": "{label} s'est etabli a {current} {unit} en {month}, soit {mom} en glissement mensuel{yoy}.",
        "fallback.labour.skilled": " {label} s'est etabli a {current} {unit}.",
        "fallback.labour.availability": " La disponibilite de la main-d'oeuvre etait {availability}.",
        "fallback.labour.purchasing_power": " Une journee de salaire permettait d'acheter environ {kg} kg de {staple} en {month}, soit {mom} en glissement mensuel.",
        "fallback.labour.driver": "L'evolution est coherente avec la demande saisonniere de main-d'oeuvre, le cycle des recoltes ou les conditions economiques generales.",
    },
    "es": {
        "report.title": "Monitor de mercados",
        "section.HIGHLIGHTS": "Aspectos destacados",
        "section.BASKET_DEFINITIONS": "Definiciones de las canastas",
        "section.MARKET_OVERVIEW": "Panorama del mercado",
        "section.COMMODITY_ANALYSIS": "Analisis de productos",
        "section.REGIONAL_HIGHLIGHTS": "Aspectos regionales destacados",
        "section.REFERENCES": "Referencias",
        "module.exchange_rate": "Analisis del tipo de cambio",
        "module.fuel_energy": "Combustibles y energia",
        "module.livestock_animal_products": "Ganado y productos animales",
        "module.labour_market": "Mercado laboral",
        "figure.food_basket_trend": "Tendencia del costo de la canasta alimentaria",
        "figure.basket_trend_role": "Tendencia del costo de {basket} ({scope})",
        "figure.basket_regional_role": "Costo regional de {basket} ({scope}, {period})",
        "figure.fuel_prices": "Tendencia de precios minoristas de combustibles",
        "figure.livestock_animal_products": "Tendencia de precios de ganado y productos animales",
        "figure.labour_market": "Tendencia del mercado laboral",
        "chart.label.current": "Actual",
        "chart.label.official": "Oficial",
        "chart.label.unofficial": "Paralelo",
        "chart.axis.cost": "Costo",
        "chart.axis.price": "Precio",
        "chart.axis.wage": "Salario",
        "chart.title.food_basket": "Tendencia del costo de la canasta alimentaria - {country}",
        "chart.title.basket_trend_role": "Tendencia del costo de {basket} - {scope} - {country}",
        "chart.title.basket_regional_role": "Costo regional de {basket} - {scope} - {period}",
        "chart.title.commodity": "Tendencias de precios de productos - {country} - {title_suffix}",
        "chart.title.exchange_rate": "Tendencia del tipo de cambio - {country}",
        "chart.title.fuel": "Precios de combustibles - {country}",
        "chart.title.livestock": "Ganado y productos animales - {country}",
        "chart.title.labour": "Mercado laboral - {country}",
        "chart.title.regional": "Costo regional de la canasta alimentaria - {period}",
        "chart.axis.currency": "{label} ({currency})",
        "chart.axis.fx": "{currency} por 1 USD",
        "chart.axis.fuel": "{currency}/litro",
        "chart.axis.unit": "{currency}/{unit}",
        "chart.axis.day": "{currency}/dia",
        "chart.axis.index_first_month": "Indice (primer mes = 100)",
        "chart.axis.kg_per_wage": "kg de {staple} por dia de salario",
        "chart.page_suffix": "{category} (pagina {page_idx}/{page_count})",
        "basket.role.primary": "Primaria",
        "basket.role.secondary": "Secundaria",
        "basket.scope.national": "Nacional",
        "basket.scope.selected_regions": "Regiones seleccionadas",
        "basket.scope.selected_regions_named": "Regiones seleccionadas: {regions}",
        "basket.table.header.basket": "Canasta / rol",
        "basket.table.header.description": "Descripcion",
        "basket.table.header.scope": "Ambito",
        "basket.table.header.composition": "Composicion",
        "commodity_category.cereals": "Cereales",
        "commodity_category.pulses": "Legumbres",
        "commodity_category.oil": "Aceite",
        "commodity_category.sugar": "Azucar",
        "commodity_category.condiments": "Condimentos",
        "commodity_category.vegetables": "Verduras",
        "commodity_category.livestock": "Ganado",
        "commodity_category.other": "Otros",
        "ui.language": "Idioma",
        "ui.language.auto": "Auto",
        "ui.language.en": "English",
        "ui.language.fr": "Francais",
        "ui.language.es": "Espanol",
        "ui.resolved_language": "Idioma del informe: {language_name}",
        "ui.run_id": "ID de ejecucion",
        "ui.country": "Pais",
        "ui.time_period": "Periodo",
        "ui.llm_calls": "Llamadas LLM",
        "ui.warnings": "Advertencias",
        "ui.qa_review": "Revision de calidad",
        "ui.qa_status": "Estado: {status}; intentos de correccion: {attempts}",
        "ui.qa_flag": "[{severity}] {section}: {details}",
        "ui.cache_metadata": "Metadatos de cache",
        "ui.report_sections": "Secciones del informe",
        "ui.visualizations": "Visualizaciones",
        "ui.data_statistics": "Estadisticas de datos",
        "ui.export_unavailable": "La exportacion solo esta disponible para ejecuciones asincronas.",
        "ui.preparing_docx": "Preparando DOCX...",
        "ui.download_docx": "Generar y descargar DOCX",
        "ui.report_ready": "Informe listo",
        "ui.report_ready_caption": "Su informe se ha generado. Descargue el archivo de Word o visualicelo en esta pagina.",
        "ui.download_report": "Descargar informe (.docx)",
        "ui.view_report": "Ver informe en esta pagina",
        "ui.hide_report": "Ocultar vista previa del informe",
        "ui.technical_details": "Detalles tecnicos",
        "ui.export_error": "No se pudo preparar el archivo de Word. Aun puede ver el informe a continuacion.",
        "ui.retry_export": "Reintentar la preparacion de la descarga",
        "live.price_data.title": "Datos de precios",
        "live.price_data.summary": "{rows} fila(s) de precios recuperada(s) para {country} ({period}) desde cache y reposiciones dirigidas.",
        "live.price_data.mock": "Los datos simulados estan habilitados para esta ejecucion; no se leyeron filas de precios.",
        "live.seerist.title": "Documentos Seerist",
        "live.reliefweb.title": "Documentos ReliefWeb",
        "live.docs.summary": "{count} documentos de {source} recuperados.",
        "live.docs.unavailable": "Recuperacion de {source} no disponible: {error}",
        "warning.partial_basket": "La canasta alimentaria de {period} se basa en {latest_count} de {selected_count} productos seleccionados con datos del mes objetivo ({latest_names}). Componentes faltantes del mes objetivo: {missing_names}.",
        "warning.exchange_stable_driver": "La clasificacion del tipo de cambio es 'stable', pero trend_analysis.key_market_drivers menciona depreciacion de la moneda. Evite afirmar depreciacion actual; si se menciona, presentela como contexto o discrepancia.",
        "warning.skip_exchange_usd": "Se omitio el modulo exchange_rate porque currency_code es USD (no hay par cambiario que recuperar).",
        "warning.skip_fuel_missing": "Se omitio el modulo fuel_energy porque no habia datos de precios de combustibles de transporte.",
        "warning.skip_livestock_missing": "Se omitio el modulo livestock_animal_products porque no habia datos de precios de ganado o productos animales.",
        "warning.skip_labour_missing": "Se omitio el modulo labour_market porque no habia datos de salarios.",
        "warning.skip_exchange_required": "Se omitio el modulo exchange_rate por entradas obligatorias faltantes: {missing}",
        "warning.skip_exchange_source": "Se omitio el modulo exchange_rate porque no habia fuente de tipo de cambio disponible: {error}",
        "warning.skip_fuel_error": "Se omitio el modulo fuel_energy porque no habia datos de precios de combustibles de transporte: {error}",
        "warning.skip_livestock_error": "Se omitio el modulo livestock_animal_products porque no habia datos de precios de ganado o productos animales: {error}",
        "warning.skip_labour_error": "Se omitio el modulo labour_market porque no habia datos de salarios: {error}",
        "warning.qa_unresolved": "La revision automatizada termino con {count} problema(s) alto(s)/medio(s) sin resolver. Revise los hallazgos estructurados antes de publicar.",
        "fallback.exchange": "El {currency} se situa en {rate} por USD.",
        "fallback.fuel.driver": "El movimiento es coherente con condiciones del mercado de combustibles, logistica o precios administrados.",
        "fallback.fuel.implication": "Los precios de los combustibles afectan los costos de transporte y distribucion, con efectos sobre los alimentos basicos, la canasta alimentaria y el poder adquisitivo de los hogares.",
        "fallback.fuel.regional": " Las diferencias regionales fueron notables, con {highest} por encima de {lowest} para {label}.",
        "fallback.fuel.metric": "{label} promedio {current} {unit} en {month}, {mom} intermensual{yoy}",
        "fallback.yoy": " y {value} interanual",
        "fallback.livestock.driver": "El movimiento es coherente con demanda estacional, oferta, alimento animal, pasturas o condiciones de agua.",
        "fallback.livestock.implication": "Estos precios influyen en la asequibilidad de proteina animal y la diversidad alimentaria de los hogares.",
        "fallback.livestock.live_animal": " Los precios de animales vivos tambien afectan los ingresos pastoriles y los terminos de intercambio ganado/cereales.",
        "fallback.livestock.regional": " Las diferencias regionales fueron notables, con {highest} por encima de {lowest} para {label}.",
        "fallback.livestock.metric": "{label} promedio {current} {unit} en {month}, {mom} intermensual{yoy}",
        "fallback.labour.metric": "{label} promedio {current} {unit} en {month}, {mom} intermensual{yoy}.",
        "fallback.labour.skilled": " {label} promedio {current} {unit}.",
        "fallback.labour.availability": " La disponibilidad de mano de obra fue {availability}.",
        "fallback.labour.purchasing_power": " Un dia de salario permitio comprar aproximadamente {kg} kg de {staple} en {month}, {mom} intermensual.",
        "fallback.labour.driver": "El movimiento es coherente con demanda laboral estacional, ciclo de cosecha o condiciones economicas generales.",
    },
}

GLOSSARY: Dict[str, Dict[str, str]] = {
    "en": {
        "food basket": "food basket",
        "terms of trade": "terms of trade",
        "lean season": "lean season",
        "exchange rate": "exchange rate",
        "price bulletin": "price bulletin",
        "purchasing power": "purchasing power",
        "staple foods": "staple foods",
        "fuel prices": "fuel prices",
        "currency depreciation": "currency depreciation",
        "livestock-to-cereal terms of trade": "livestock-to-cereal terms of trade",
    },
    "fr": {
        "food basket": "panier alimentaire",
        "terms of trade": "termes de l'echange",
        "lean season": "periode de soudure",
        "exchange rate": "taux de change",
        "price bulletin": "bulletin des prix",
        "purchasing power": "pouvoir d'achat",
        "staple foods": "denrees alimentaires de base",
        "fuel prices": "prix des carburants",
        "currency depreciation": "depreciation de la monnaie",
        "livestock-to-cereal terms of trade": "termes de l'echange betail/cereales",
    },
    "es": {
        "food basket": "canasta alimentaria",
        "terms of trade": "terminos de intercambio",
        "lean season": "temporada de escasez",
        "exchange rate": "tipo de cambio",
        "price bulletin": "boletin de precios",
        "purchasing power": "poder adquisitivo",
        "staple foods": "alimentos basicos",
        "fuel prices": "precios de los combustibles",
        "currency depreciation": "depreciacion de la moneda",
        "livestock-to-cereal terms of trade": "terminos de intercambio ganado/cereales",
    },
}


def normalize_language(language: Optional[str], *, allow_auto: bool = False) -> str:
    candidate = str(language or "auto").strip().lower()
    allowed = set(SUPPORTED_REPORT_LANGUAGES)
    if allow_auto:
        allowed.add("auto")
    if candidate not in allowed:
        raise ValueError(f"Unsupported report language: {language}")
    return candidate


def locale_for_language(language: Optional[str]) -> str:
    lang = normalize_language(language or "en")
    return LANGUAGE_LOCALES[lang]


def resolve_report_language(country: str, requested_language: Optional[str] = "auto") -> Dict[str, str]:
    requested = normalize_language(requested_language or "auto", allow_auto=True)
    if requested != "auto":
        return {
            "language": requested,
            "locale": locale_for_language(requested),
            "language_source": "explicit",
        }

    try:
        _canonical, iso3 = resolve_country(country)
    except Exception:
        iso3 = ""

    if iso3 in FRENCH_DEFAULT_ISO3:
        language = "fr"
        source = "country_default"
    elif iso3 in SPANISH_DEFAULT_ISO3:
        language = "es"
        source = "country_default"
    else:
        language = "en"
        source = "default"

    return {
        "language": language,
        "locale": locale_for_language(language),
        "language_source": source,
    }


def t(language: Optional[str], key: str, **params: Any) -> str:
    lang = normalize_language(language or "en")
    table = STRINGS.get(lang) or STRINGS["en"]
    text = table.get(key) or STRINGS["en"].get(key)
    if text is None:
        raise KeyError(f"Missing localized string: {lang}.{key}")
    if params:
        return text.format(**params)
    return text


def glossary_text(language: Optional[str]) -> str:
    lang = normalize_language(language or "en")
    terms = GLOSSARY[lang]
    return "\n".join(f"- {source}: {target}" for source, target in terms.items())


def locale_instruction(language: Optional[str]) -> str:
    lang = normalize_language(language or "en")
    if lang == "fr":
        return (
            "Use French locale formatting: decimal comma, space for thousands, a space before %, "
            "and month names in French (for example: 2 307,0 CDF; 1,5 %; mai 2026)."
        )
    if lang == "es":
        return (
            "Use Spanish locale formatting: decimal comma, period for thousands, a space before %, "
            "and month names in Spanish (for example: 2.307,0 CDF; 1,5 %; mayo 2026)."
        )
    return (
        "Use English locale formatting: decimal point, comma for thousands, no space before %, "
        "and English month labels (for example: 2,307.0 CDF; 1.5%; May 2026)."
    )


def prompt_base_context(language: Optional[str]) -> Dict[str, str]:
    lang = normalize_language(language or "en")
    return {
        "language_name": LANGUAGE_NAMES[lang],
        "locale_instruction": locale_instruction(lang),
        "terminology_glossary": glossary_text(lang),
    }


def _clean_spaces(text: str) -> str:
    return str(text).replace("\u00a0", " ").replace("\u202f", " ")


def _to_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        if isinstance(value, Decimal):
            return value
        if isinstance(value, (int, float)):
            return Decimal(str(value))
        text = _clean_spaces(str(value)).strip()
        if not text or text.lower() in {"nan", "none", "n/a"}:
            return None
        text = text.replace(" ", "")
        if "," in text and "." in text:
            if text.rfind(",") > text.rfind("."):
                text = text.replace(".", "").replace(",", ".")
            else:
                text = text.replace(",", "")
        elif "," in text:
            parts = text.split(",")
            if len(parts[-1]) == 3 and all(part.isdigit() for part in parts):
                text = "".join(parts)
            else:
                text = text.replace(",", ".")
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def format_decimal_value(value: Any, language: Optional[str] = "en", *, decimals: int = 1) -> str:
    number = _to_decimal(value)
    if number is None:
        return str(value)
    fmt = "#,##0" if decimals <= 0 else "#,##0." + ("0" * decimals)
    return _clean_spaces(format_decimal(number, format=fmt, locale=locale_for_language(language)))


def format_percent_value(value: Any, language: Optional[str] = "en", *, include_arrow: bool = False) -> str:
    number = _to_decimal(value)
    if number is None:
        return "N/A"
    arrow = ""
    if include_arrow:
        if number > 0:
            arrow = "\u2191"
        elif number < 0:
            arrow = "\u2193"
    formatted = format_decimal_value(abs(number) if include_arrow else number, language, decimals=1)
    if normalize_language(language or "en") == "en":
        return f"{arrow}{formatted}%"
    return f"{arrow}{formatted} %"


def format_currency_value(
    value: Any,
    currency_code: Optional[str],
    language: Optional[str] = "en",
    *,
    decimals: int = 1,
) -> str:
    number = _to_decimal(value)
    code = str(currency_code or "LCU").strip().upper() or "LCU"
    if number is None:
        return f"{value} {code}".strip()
    fmt = "#,##0" if decimals <= 0 else "#,##0." + ("0" * decimals)
    try:
        return _clean_spaces(
            format_currency(
                number,
                code,
                format=f"{fmt} \u00a4\u00a4",
                currency_digits=False,
                locale=locale_for_language(language),
            )
        )
    except Exception:
        return f"{format_decimal_value(number, language, decimals=decimals)} {code}"


def format_month_label(value: Any, language: Optional[str] = "en", *, width: str = "wide") -> str:
    if value in (None, ""):
        return ""
    try:
        if isinstance(value, pd.Timestamp):
            dt = value.to_pydatetime().date()
        elif isinstance(value, datetime):
            dt = value.date()
        elif isinstance(value, date):
            dt = value
        else:
            text = str(value).strip()
            if re.match(r"^\d{4}-\d{2}$", text):
                text = f"{text}-01"
            dt = pd.to_datetime(text, errors="raise").date()
        pattern = "MMM y" if width == "abbrev" else "MMMM y"
        return _clean_spaces(format_date(dt, format=pattern, locale=locale_for_language(language)))
    except Exception:
        return str(value)


def localize_axis_label(axis_label: Any, language: Optional[str] = "en") -> str:
    text = str(axis_label or "").strip()
    if not text:
        return text
    if text == "Index (first month = 100)":
        return t(language, "chart.axis.index_first_month")
    m = re.match(r"^kg of (.+) per day's wage$", text, flags=re.IGNORECASE)
    if m:
        return t(language, "chart.axis.kg_per_wage", staple=m.group(1))
    m = re.match(r"^([A-Z]{3})/Litre$", text)
    if m:
        return t(language, "chart.axis.fuel", currency=m.group(1))
    m = re.match(r"^([A-Z]{3})/day$", text, flags=re.IGNORECASE)
    if m:
        return t(language, "chart.axis.day", currency=m.group(1))
    return text


def _shield(text: str, patterns: Iterable[str], protected: List[str]) -> str:
    def repl(match: re.Match[str]) -> str:
        protected.append(match.group(0))
        return f"@@MM_PROTECTED_{len(protected) - 1}@@"

    out = text
    for pattern in patterns:
        out = re.sub(pattern, repl, out)
    return out


def _restore(text: str, protected: List[str]) -> str:
    out = text
    for idx, value in enumerate(protected):
        out = out.replace(f"@@MM_PROTECTED_{idx}@@", value)
    return out


def normalize_generated_text(
    text: Any,
    language: Optional[str] = "en",
    *,
    reference_titles: Optional[Iterable[str]] = None,
) -> Tuple[str, List[str]]:
    """Normalize obvious generated numeric tokens without touching protected references."""
    lang = normalize_language(language or "en")
    value = str(text or "")
    if lang == "en" or not value:
        return value, []

    warnings: List[str] = []
    protected: List[str] = []
    try:
        patterns = [
            r"https?://\S+",
            r"\[INSERT GRAPH:\s*[A-Za-z0-9_\-]+\s*\]",
            r"\[[A-Za-z0-9_\-:.]+\]",
            r"\b\d{4}-\d{2}-\d{2}\b",
            r"\b\d{4}-\d{2}\b",
            r"\b(?:19|20)\d{2}\b",
        ]
        work = _shield(value, patterns, protected)
        for title in reference_titles or []:
            title_text = str(title or "").strip()
            if title_text and title_text in work:
                protected.append(title_text)
                work = work.replace(title_text, f"@@MM_PROTECTED_{len(protected) - 1}@@")

        def pct_repl(match: re.Match[str]) -> str:
            parsed = _to_decimal(match.group("number"))
            if parsed is None:
                return match.group(0)
            return format_percent_value(parsed, lang, include_arrow=False)

        work = re.sub(
            r"(?<![\w@])(?P<number>[+-]?\d+(?:[.,]\d+)?)\s*%",
            pct_repl,
            work,
        )

        def currency_repl(match: re.Match[str]) -> str:
            parsed = _to_decimal(match.group("number"))
            code = match.group("code")
            if parsed is None:
                return match.group(0)
            return format_currency_value(parsed, code, lang, decimals=1)

        work = re.sub(
            r"(?<![\w@])(?P<number>[+-]?\d[\d\s,\.]*\d|[+-]?\d)\s+(?P<code>[A-Z]{3})(?![A-Z])",
            currency_repl,
            work,
        )

        def decimal_repl(match: re.Match[str]) -> str:
            parsed = _to_decimal(match.group("number"))
            if parsed is None:
                return match.group(0)
            decimals = 1 if "." in match.group("number") or "," in match.group("number") else 0
            return format_decimal_value(parsed, lang, decimals=decimals)

        work = re.sub(
            r"(?<![\w@])(?P<number>[+-]?(?:\d{1,3}(?:[,.]\d{3})+|\d+[,.]\d+))(?![\w@])",
            decimal_repl,
            work,
        )

        return _restore(work, protected), warnings
    except Exception as exc:
        logger.warning("Generated text normalization failed: %s", exc)
        warnings.append(f"Localization normalization failed: {exc}")
        return value, warnings
