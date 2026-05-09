"""
Nefkens Private Lease Monitor
====================================
Draait dagelijks via GitHub Actions.
E-mailgegevens worden veilig opgehaald uit GitHub Secrets.
"""

import json
import os
import re
import smtplib
import logging
from pathlib import Path
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import unquote
import time

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By

# ─────────────────────────────────────────────
# CONFIGURATIE
# ─────────────────────────────────────────────

EMAIL_CONFIG = {
    "smtp_server":  "smtp.gmail.com",
    "smtp_port":    587,
    "username":     os.environ.get("GMAIL_USERNAME", ""),
    "password":     os.environ.get("GMAIL_PASSWORD", ""),
    "from_address": os.environ.get("GMAIL_USERNAME", ""),
    "to_addresses": ["jordie.hagen@nefkens.nl"],
}

MERKEN = [
    {"naam": "Peugeot",        "url": "https://privatelease.peugeot.nl/modellen"},
    {"naam": "Citroën",        "url": "https://privatelease.citroen.nl/modellen"},
    {"naam": "DS Automobiles", "url": "https://privatelease.dsautomobiles.nl/modellen"},
    {"naam": "Opel",           "url": "https://privatelease.opel.nl/modellen"},
    {"naam": "Fiat",           "url": "https://privatelease.fiat.nl/modellen"},
    {"naam": "Alfa Romeo",     "url": "https://privatelease.alfaromeo.nl/modellen"},
    {"naam": "Jeep",           "url": "https://privatelease.jeep.nl/modellen"},
    {"naam": "Abarth",         "url": "https://privatelease.abarth.nl/modellen"},
    {"naam": "Lancia",         "url": "https://privatelease.lancia.nl/modellen"},
    {"naam": "Leapmotor",      "url": "https://privatelease.leapmotor.nl/modellen"},
]

# Merken waarbij we via de configurator onderscheid maken tussen elektrisch en overig
CONFIGURATOR_MERKEN = {"Alfa Romeo", "Jeep"}

# Termen die duiden op een elektrische aandrijving
ELEKTRISCH_TERMEN = {"elektrisch", "electric", "bev", "ev"}

DATA_FILE = Path("nefkens_prices.json")
LOG_FILE  = Path("nefkens_monitor.log")

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# BROWSER
# ─────────────────────────────────────────────

def get_driver():
    options = webdriver.ChromeOptions()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.binary_location = "/usr/bin/chromium-browser"
    service = Service("/usr/bin/chromedriver")
    return webdriver.Chrome(service=service, options=options)

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def normaliseer_prijs(prijs_tekst):
    """Normaliseert een prijs naar '€ XXX,-' formaat."""
    if not prijs_tekst:
        return None
    prijs_tekst = prijs_tekst.replace("\u00a0", " ").strip()
    m = re.search(r"€\s*([\d.]+(?:,\d+)?)", prijs_tekst)
    if not m:
        return prijs_tekst
    getal = m.group(1).rstrip(".,")
    return f"€ {getal},-"


def haal_prijs_uit_pagina(driver):
    """
    Haalt de eerste redelijke lease-prijs (€ 150-1500/mnd) van de huidige pagina.
    Geeft een genormaliseerde prijs terug, of None als niks gevonden.
    """
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text
        for m in re.finditer(r"€\s*([\d.]+(?:,\d+)?)", body_text):
            getal_str = m.group(1).replace(".", "").replace(",", "")
            try:
                if 150 <= int(getal_str[:4]) <= 1500:
                    return normaliseer_prijs(f"€ {m.group(1)}")
            except (ValueError, IndexError):
                continue
    except Exception:
        pass
    return None


def prijs_naar_float(prijs_str):
    """Zet '€ 468,99,-' om naar float voor numerieke vergelijking."""
    if not prijs_str:
        return float("inf")
    m = re.search(r"€\s*([\d.]+(?:,\d+)?)", prijs_str)
    if not m:
        return float("inf")
    try:
        return float(m.group(1).replace(".", "").replace(",", "."))
    except ValueError:
        return float("inf")

# ─────────────────────────────────────────────
# CONFIGURATOR SCRAPER (Alfa Romeo & Jeep)
# ─────────────────────────────────────────────

def vind_configurator_link(driver):
    """
    Zoekt op de huidige modelpagina naar een link naar de configurator.
    Herkent zowel 'Stel zelf samen'-tekst als href's met 'configurator'.
    """
    for el in driver.find_elements(By.TAG_NAME, "a"):
        href = el.get_attribute("href") or ""
        tekst = el.text.strip().lower()
        if "configurator" in href.lower():
            return href
        if "stel" in tekst and "samen" in tekst and href:
            return href
    return None


def vind_brandstof_opties(driver):
    """
    Zoekt selecteerbare motorisatie-opties in de configurator.
    Geeft een lijst van (naam, element) terug, zonder dubbelen en
    zonder navigatie-links (bijv. 'ontdek elektrisch rijden').
    """
    LABELS = [
        "Elektrisch", "Electric", "BEV",
        "Plug-in Hybrid", "PHEV",
        "Mild Hybrid", "MHEV",
        "Hybride", "Hybrid",
        "Benzine", "Petrol",
        "Diesel",
    ]
    SKIP_HREF_SUBSTRINGS = [
        "elektrisch-rijden", "hybride-rijden",
        "diesel-rijden", "benzine-rijden",
    ]
    KLIKBARE_TAGS = {"button", "label", "span", "div", "li", "a", "input"}

    gevonden = []
    gevonden_namen = set()

    for label in LABELS:
        try:
            els = driver.find_elements(
                By.XPATH,
                f"//*[normalize-space(.)='{label}' or normalize-space(text())='{label}']",
            )
            for el in els:
                if el.tag_name.lower() not in KLIKBARE_TAGS:
                    continue
                href = el.get_attribute("href") or ""
                if any(s in href for s in SKIP_HREF_SUBSTRINGS):
                    continue
                naam = el.text.strip() or label
                if naam and naam not in gevonden_namen:
                    gevonden.append((naam, el))
                    gevonden_namen.add(naam)
        except Exception:
            continue

    return gevonden


def scrape_configurator_prijzen(driver, model_naam, model_url):
    """
    Volgt de 'Stel zelf samen' link van de modelpagina naar de configurator
    en haalt per brandstoftype de vanafprijs op.
    Geeft een dict terug: {'Elektrisch': '€ 469,-', 'Overig': '€ 395,-'}
    """
    prijzen = {}

    try:
        # Stap 1: modelpagina laden en configurator-link vinden
        driver.get(model_url)
        time.sleep(5)
        log.info("  -> Modelpagina geladen: %s", model_url)

        configurator_url = vind_configurator_link(driver)
        if not configurator_url:
            log.warning("  -> %s: geen 'Stel zelf samen' link gevonden", model_naam)
            return prijzen

        # Stap 2: configurator laden
        driver.get(configurator_url)
        time.sleep(8)
        log.info("  -> Configurator geladen: %s", driver.current_url)

        snippet = driver.find_element(By.TAG_NAME, "body").text[:300].replace("\n", " ")
        log.info("  -> Snippet: %s", snippet)

        # Stap 3: beginprijs lezen (fallback als er geen tabs zijn)
        begin_prijs = haal_prijs_uit_pagina(driver)
        log.info("  -> Beginprijs: %s", begin_prijs)

        # Stap 4: brandstof-/motorisatie-opties zoeken
        opties = vind_brandstof_opties(driver)
        log.info("  -> Opties gevonden: %s", [o[0] for o in opties])

        if not opties:
            # Geen keuze-opties = enkelvoudig model; beginprijs is de enige prijs
            if begin_prijs:
                prijzen["Overig"] = begin_prijs
            return prijzen

        # Stap 5: per optie klikken en prijs ophalen
        elektrisch_prijzen = []
        overig_prijzen = []

        for opt_naam, opt_el in opties:
            try:
                driver.execute_script("arguments[0].click();", opt_el)
                time.sleep(4)

                prijs = haal_prijs_uit_pagina(driver)
                if not prijs:
                    log.warning("    -> '%s': geen prijs na klik", opt_naam)
                    continue

                log.info("    -> '%s': %s", opt_naam, prijs)

                eerste_woord = opt_naam.lower().split()[0] if opt_naam else ""
                if eerste_woord in ELEKTRISCH_TERMEN:
                    elektrisch_prijzen.append(prijs)
                else:
                    overig_prijzen.append(prijs)

            except Exception as e:
                log.warning("    -> Fout bij optie '%s': %s", opt_naam, e)

        # Stap 6: resultaten samenstellen
        if elektrisch_prijzen:
            prijzen["Elektrisch"] = min(elektrisch_prijzen, key=prijs_naar_float)

        if overig_prijzen:
            prijzen["Overig"] = min(overig_prijzen, key=prijs_naar_float)
        elif not elektrisch_prijzen and begin_prijs:
            prijzen["Overig"] = begin_prijs

    except Exception as e:
        log.error("  -> Fout bij configurator %s: %s", model_naam, e)

    return prijzen

# ─────────────────────────────────────────────
# HOOFD SCRAPER
# ─────────────────────────────────────────────

def scrape_merk(driver, merk):
    log.info("=" * 50)
    log.info("Scrapen: %s", merk["naam"])
    prijzen = {}

    try:
        # Stap 1: overzichtspagina laden en alle model-URLs verzamelen
        driver.get(merk["url"])
        time.sleep(5)

        links = driver.find_elements(By.CSS_SELECTOR, "a[href*='/modellen/']")

        model_urls = {}
        for link in links:
            href = link.get_attribute("href") or ""
            parts = href.split("/modellen/")
            if len(parts) != 2:
                continue
            # Verwijder eventuele trailing slash; modelnaam mag NIET nog een '/' bevatten
            naam_raw = parts[1].rstrip("/")
            if not naam_raw or "/" in naam_raw:
                continue
            if "voorraad" in href or "occasions" in href:
                continue
            # URL-decoderen: 'e-2008%20SUV' → 'e-2008 SUV', 'N%C2%B04' → 'N°4'
            model_naam = unquote(naam_raw)
            model_urls[model_naam] = href.rstrip("/")

        log.info("  -> %d modellen gevonden: %s", len(model_urls), list(model_urls.keys()))

        # Stap 2: bezoek elke modelpagina
        for model_naam, model_url in model_urls.items():
            try:
                if merk["naam"] in CONFIGURATOR_MERKEN:
                    # Configurator-aanpak: elektrisch + overig via motorisatie-tabs
                    config_prijzen = scrape_configurator_prijzen(driver, model_naam, model_url)
                    if config_prijzen:
                        for aandrijving, prijs in config_prijzen.items():
                            prijzen[f"{model_naam} ({aandrijving})"] = prijs
                    else:
                        log.warning("  -> %s: configurator gaf geen resultaat", model_naam)

                else:
                    # Standaard merken: 'Stel zelf samen' prijs van modelpagina
                    driver.get(model_url)
                    time.sleep(4)

                    page_text = driver.find_element(By.TAG_NAME, "body").text
                    prijs = None

                    # Zoek 'Stel zelf samen' prijs (meest specifiek)
                    m = re.search(
                        r"[Ss]tel\s+zelf\s+samen[^€]*(€\s*[\d.]+(?:,\d+)?)",
                        page_text, re.IGNORECASE,
                    )
                    if m:
                        prijs = normaliseer_prijs(m.group(1))

                    # Fallback: eerste € bedrag in lease-range
                    if not prijs:
                        for m in re.finditer(r"€\s*([\d.]+(?:,\d+)?)", page_text):
                            getal_str = m.group(1).replace(".", "").replace(",", "")
                            try:
                                if 150 <= int(getal_str[:4]) <= 1500:
                                    prijs = normaliseer_prijs(f"€ {m.group(1)}")
                                    break
                            except (ValueError, IndexError):
                                continue

                    if prijs:
                        prijzen[model_naam] = prijs
                        log.info("  -> %s: %s", model_naam, prijs)
                    else:
                        log.warning("  -> %s: geen prijs gevonden", model_naam)

            except Exception as e:
                log.error("  -> Fout bij model '%s': %s", model_naam, e)

            time.sleep(2)

    except Exception as e:
        log.error("  -> Fout bij merk '%s': %s", merk["naam"], e)

    log.info("  -> Klaar: %d modellen voor %s", len(prijzen), merk["naam"])
    return prijzen

# ─────────────────────────────────────────────
# OPSLAG & VERGELIJKING
# ─────────────────────────────────────────────

def laad_opgeslagen():
    if DATA_FILE.exists():
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        return data.get("prijzen", {})
    return {}


def sla_op(alle_prijzen):
    DATA_FILE.write_text(
        json.dumps(
            {"bijgewerkt_op": datetime.now().isoformat(), "prijzen": alle_prijzen},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def vergelijk(oud, nieuw):
    wijzigingen = []
    for merk, modellen in nieuw.items():
        oud_merk = oud.get(merk, {})
        for model, prijs in modellen.items():
            if model not in oud_merk:
                wijzigingen.append({"merk": merk, "model": model, "type": "Nieuw",      "oud": "—",             "nieuw": prijs})
            elif oud_merk[model] != prijs:
                wijzigingen.append({"merk": merk, "model": model, "type": "Gewijzigd",  "oud": oud_merk[model], "nieuw": prijs})
        for model in oud_merk:
            if model not in modellen:
                wijzigingen.append({"merk": merk, "model": model, "type": "Verwijderd", "oud": oud_merk[model], "nieuw": "—"})
    return wijzigingen

# ─────────────────────────────────────────────
# E-MAIL
# ─────────────────────────────────────────────

def bouw_email_html(wijzigingen, alle_prijzen):
    datum = datetime.now().strftime("%d-%m-%Y %H:%M")
    n = len(wijzigingen)

    per_merk = {}
    for w in wijzigingen:
        per_merk.setdefault(w["merk"], []).append(w)

    secties = ""
    for merk, items in per_merk.items():
        rijen = ""
        for w in items:
            kleur = {"Nieuw": "#d4edda", "Gewijzigd": "#fff3cd", "Verwijderd": "#f8d7da"}[w["type"]]
            icoon = {"Nieuw": "🟢", "Gewijzigd": "🟡", "Verwijderd": "🔴"}[w["type"]]
            rijen += f"""<tr style="background:{kleur}">
                <td style="padding:8px">{w['model']}</td>
                <td style="padding:8px">{icoon} {w['type']}</td>
                <td style="padding:8px">{w['oud']}</td>
                <td style="padding:8px"><strong>{w['nieuw']}</strong></td>
            </tr>"""
        secties += f"""
            <h3 style="margin-top:24px;border-bottom:2px solid #eee;padding-bottom:4px">{merk}</h3>
            <table border="1" cellspacing="0" style="border-collapse:collapse;width:100%;margin-bottom:16px">
                <tr style="background:#f0f0f0">
                    <th style="padding:8px;text-align:left">Model</th>
                    <th style="padding:8px;text-align:left">Status</th>
                    <th style="padding:8px;text-align:left">Oude prijs</th>
                    <th style="padding:8px;text-align:left">Nieuwe prijs</th>
                </tr>{rijen}
            </table>"""

    overzicht = ""
    for merk, modellen in sorted(alle_prijzen.items()):
        if not modellen:
            continue
        rijen = "".join(
            f'<tr><td style="padding:6px">{m}</td><td style="padding:6px">{p}</td></tr>'
            for m, p in sorted(modellen.items())
        )
        overzicht += f"""
            <h3 style="margin-top:20px">{merk}</h3>
            <table border="1" cellspacing="0" style="border-collapse:collapse;margin-bottom:12px">
                <tr style="background:#f0f0f0">
                    <th style="padding:6px;text-align:left">Model</th>
                    <th style="padding:6px;text-align:left">Vanafprijs/mnd</th>
                </tr>{rijen}
            </table>"""

    wijzigingen_blok = f"""
        <h2>Wijzigingen ({n})</h2>
        {secties if n > 0 else '<p style="color:#666">Geen wijzigingen t.o.v. de vorige meting.</p>'}
    """

    return f"""<html><body style="font-family:Arial,sans-serif;color:#333;max-width:800px">
        <h2 style="color:#1B4F8A">Nefkens Private Lease Monitor</h2>
        <p>Gecontroleerd op: <strong>{datum}</strong></p>
        {wijzigingen_blok}
        <h2 style="margin-top:40px">Volledig actueel overzicht</h2>
        {overzicht}
        <p style="color:#aaa;font-size:11px;margin-top:40px;border-top:1px solid #eee;padding-top:12px">
            Automatisch bericht · Nefkens Private Lease Monitor · GitHub Actions · {datum}
        </p>
    </body></html>"""


def stuur_email(wijzigingen, alle_prijzen):
    cfg   = EMAIL_CONFIG
    datum = datetime.now().strftime("%d-%m-%Y")
    n     = len(wijzigingen)
    subject = f"[Nefkens Private Lease Monitor] {n} wijziging{'en' if n != 1 else ''} - {datum}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = cfg["from_address"]
    msg["To"]      = ", ".join(cfg["to_addresses"])
    msg.attach(MIMEText(bouw_email_html(wijzigingen, alle_prijzen), "html", "utf-8"))

    with smtplib.SMTP(cfg["smtp_server"], cfg["smtp_port"]) as server:
        server.starttls()
        server.login(cfg["username"], cfg["password"])
        server.sendmail(cfg["from_address"], cfg["to_addresses"], msg.as_string())
    log.info("E-mail verstuurd naar: %s", ", ".join(cfg["to_addresses"]))

# ─────────────────────────────────────────────
# HOOFDPROGRAMMA
# ─────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info("Nefkens Private Lease Monitor gestart - %s",
             datetime.now().strftime("%d-%m-%Y %H:%M"))
    log.info("=" * 60)

    oude_prijzen   = laad_opgeslagen()
    nieuwe_prijzen = {}

    driver = get_driver()
    try:
        for merk in MERKEN:
            nieuwe_prijzen[merk["naam"]] = scrape_merk(driver, merk)
            time.sleep(2)
    finally:
        driver.quit()

    wijzigingen = vergelijk(oude_prijzen, nieuwe_prijzen)
    sla_op(nieuwe_prijzen)

    totaal = sum(len(m) for m in nieuwe_prijzen.values())
    log.info("Totaal: %d modellen over %d merken", totaal, len(MERKEN))

    if wijzigingen:
        log.info("%d wijziging(en) — e-mail wordt verstuurd", len(wijzigingen))
        stuur_email(wijzigingen, nieuwe_prijzen)
    else:
        log.info("Geen wijzigingen — geen e-mail verstuurd")

    log.info("Klaar.\n")


if __name__ == "__main__":
    main()
