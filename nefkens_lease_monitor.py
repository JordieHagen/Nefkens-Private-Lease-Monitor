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

# Merken waarbij we via de configurator elektrisch/overig onderscheiden
CONFIGURATOR_MERKEN = {"Alfa Romeo", "Jeep"}

# Eerste woorden die duiden op een elektrische aandrijving
ELEKTRISCH_TERMEN = {"elektrisch", "electric", "bev", "ev", "e-tense"}

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
    prijs_tekst = prijs_tekst.replace(" ", " ").strip()
    m = re.search(r"€\s*([\d.]+(?:,\d+)?)", prijs_tekst)
    if not m:
        return prijs_tekst
    getal = m.group(1).rstrip(".,")
    return f"€ {getal},-"


def is_lease_prijs(bedrag_str):
    """
    Controleert of een bedrag een redelijke lease-maandprijs is (€ 150-1500).
    Gebruikt het integer-deel zodat bedragen met centen (€ 468,99) correct werken.
    """
    integer_str = bedrag_str.split(",")[0].replace(".", "")
    try:
        return 150 <= int(integer_str) <= 1500
    except (ValueError, IndexError):
        return False


def haal_prijs_uit_pagina(driver):
    """Haalt de eerste redelijke lease-prijs van de huidige pagina."""
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text
        for m in re.finditer(r"€\s*([\d.]+(?:,\d+)?)", body_text):
            if is_lease_prijs(m.group(1)):
                return normaliseer_prijs(f"€ {m.group(1)}")
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


def vind_model_links(driver, basis_url):
    """
    Haalt alle model-links op van de huidige pagina.
    Geeft een dict {model_naam: href} terug.
    """
    model_urls = {}
    for link in driver.find_elements(By.CSS_SELECTOR, "a[href*='/modellen/']"):
        href = link.get_attribute("href") or ""
        parts = href.split("/modellen/")
        if len(parts) != 2:
            continue
        naam_raw = parts[1].rstrip("/")
        if not naam_raw or "/" in naam_raw:
            continue
        if "voorraad" in href or "occasions" in href:
            continue
        model_naam = unquote(naam_raw)
        model_urls[model_naam] = href.rstrip("/")
    return model_urls

# ─────────────────────────────────────────────
# OVERZICHTSPAGINA SCRAPER (standaard merken)
# ─────────────────────────────────────────────

def scrape_overzicht_prijzen(driver, merk):
    """
    Scrapet de 'vanaf' prijzen direct van de overzichtspagina /modellen.
    Zoekt per model-card de prijs in het omliggende DOM-element via JavaScript.
    Werkt voor: Peugeot, Citroën, DS, Opel, Fiat, Abarth, Lancia, Leapmotor.
    """
    prijzen = {}

    try:
        driver.get(merk["url"])
        time.sleep(5)
        log.info("  -> Overzichtspagina geladen: %s", merk["url"])

        links = driver.find_elements(By.CSS_SELECTOR, "a[href*='/modellen/']")
        verwerkt = set()

        for link in links:
            href = link.get_attribute("href") or ""
            parts = href.split("/modellen/")
            if len(parts) != 2:
                continue
            naam_raw = parts[1].rstrip("/")
            if not naam_raw or "/" in naam_raw:
                continue
            if "voorraad" in href or "occasions" in href:
                continue

            model_naam = unquote(naam_raw)
            if model_naam in verwerkt:
                continue
            verwerkt.add(model_naam)

            # Zoek de prijs in het omliggende kaart-element via JavaScript DOM-traversal
            bedrag = driver.execute_script("""
                var link = arguments[0];
                var el = link.parentElement;
                for (var i = 0; i < 8; i++) {
                    if (!el) break;
                    var text = el.innerText || '';
                    var matches = text.match(/€\\s*([\\d.]+(?:,[\\d]+)?)/g);
                    if (matches) {
                        for (var j = 0; j < matches.length; j++) {
                            var m = matches[j].match(/€\\s*([\\d.]+(?:,[\\d]+)?)/);
                            if (!m) continue;
                            var intStr = m[1].split(',')[0].replace(/\\./g, '');
                            var val = parseInt(intStr);
                            if (val >= 150 && val <= 1500) return m[1];
                        }
                    }
                    el = el.parentElement;
                }
                return null;
            """, link)

            if bedrag:
                prijzen[model_naam] = normaliseer_prijs(f"€ {bedrag}")
                log.info("  -> %s: %s", model_naam, prijzen[model_naam])
            else:
                log.warning("  -> %s: geen prijs gevonden op overzichtspagina", model_naam)

    except Exception as e:
        log.error("  -> Fout bij overzichtspagina %s: %s", merk["naam"], e)

    log.info("  -> Klaar: %d modellen voor %s", len(prijzen), merk["naam"])
    return prijzen

# ─────────────────────────────────────────────
# VOORRAAD SCRAPER (fallback voor configurator)
# ─────────────────────────────────────────────

def scrape_voorraad_prijzen(driver, model_naam):
    """
    Leest de goedkoopste elektrische en niet-elektrische prijs
    van de huidige voorraad-pagina.
    Gebruikt als fallback wanneer geen 'Stel zelf samen' configurator gevonden.
    """
    elektrisch_prijzen = []
    overig_prijzen = []

    ELEKTRISCH_KW = {"elektrisch", "electric", "bev", "ev", "e-tense", "full electric"}
    OVERIG_KW     = {"benzine", "diesel", "hybrid", "mhev", "phev", "plug-in",
                     "mild hybrid", "plug-in hybrid"}

    try:
        time.sleep(5)
        log.info("  -> Voorraadpagina: %s", driver.current_url)

        page_text = driver.find_element(By.TAG_NAME, "body").text
        lines = [l.strip() for l in page_text.split("\n") if l.strip()]

        for i, line in enumerate(lines):
            prijs_m = re.search(r"€\s*([\d.]+(?:,\d+)?)", line)
            if not prijs_m or not is_lease_prijs(prijs_m.group(1)):
                continue

            prijs = normaliseer_prijs(f"€ {prijs_m.group(1)}")
            # Bekijk context: 6 regels voor en na de prijs
            context = " ".join(lines[max(0, i - 6): i + 6]).lower()

            if any(kw in context for kw in ELEKTRISCH_KW):
                elektrisch_prijzen.append(prijs)
            else:
                overig_prijzen.append(prijs)

    except Exception as e:
        log.error("  -> Fout bij voorraad %s: %s", model_naam, e)

    resultaat = {}
    if elektrisch_prijzen:
        resultaat["Elektrisch"] = min(elektrisch_prijzen, key=prijs_naar_float)
        log.info("  -> %s Elektrisch (goedkoopste voorraad): %s", model_naam, resultaat["Elektrisch"])
    if overig_prijzen:
        resultaat["Overig"] = min(overig_prijzen, key=prijs_naar_float)
        log.info("  -> %s Overig (goedkoopste voorraad): %s", model_naam, resultaat["Overig"])

    return resultaat

# ─────────────────────────────────────────────
# CONFIGURATOR SCRAPER (Alfa Romeo & Jeep)
# ─────────────────────────────────────────────

def vind_configurator_link(driver):
    """Zoekt een link naar de configurator op de huidige pagina."""
    for el in driver.find_elements(By.TAG_NAME, "a"):
        href = el.get_attribute("href") or ""
        tekst = el.text.strip().lower()
        if "configurator" in href.lower():
            return href
        if "stel" in tekst and "samen" in tekst and href:
            return href
    return None


def vind_voorraad_link(driver):
    """Zoekt een link naar de voorraadpagina op de huidige pagina."""
    for el in driver.find_elements(By.TAG_NAME, "a"):
        href = el.get_attribute("href") or ""
        tekst = el.text.strip().lower()
        if "voorraad" in href.lower() and "modellen" not in href.lower():
            return href
        if "bekijk" in tekst and "voorraad" in tekst and href:
            return href
    return None


def vind_brandstof_opties(driver):
    """
    Zoekt selecteerbare motorisatie-opties in de configurator.
    Geeft lijst van namen terug (geen elementen, om stale references te voorkomen).
    """
    LABELS = [
        "Elektrisch", "Electric", "BEV", "ELEKTRISCH",
        "Plug-in Hybrid", "PLUG-IN HYBRID", "PHEV",
        "Mild Hybrid", "MILD HYBRID", "MHEV",
        "Hybride", "Hybrid",
        "Benzine", "Petrol",
        "Diesel",
    ]
    SKIP_HREF = ["elektrisch-rijden", "hybride-rijden", "diesel-rijden", "benzine-rijden"]
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
                if any(s in href for s in SKIP_HREF):
                    continue
                naam = el.text.strip() or label
                if naam and naam not in gevonden_namen:
                    gevonden.append(naam)
                    gevonden_namen.add(naam)
        except Exception:
            continue

    return gevonden


def klik_brandstof_optie(driver, naam):
    """
    Hervindt en klikt een brandstof-optie op naam.
    Hervindt het element elke keer om stale references te vermijden.
    """
    KLIKBARE_TAGS = {"button", "label", "span", "div", "li", "a", "input"}
    SKIP_HREF = ["elektrisch-rijden", "hybride-rijden", "diesel-rijden", "benzine-rijden"]

    try:
        els = driver.find_elements(
            By.XPATH,
            f"//*[normalize-space(.)='{naam}' or normalize-space(text())='{naam}']",
        )
        for el in els:
            if el.tag_name.lower() not in KLIKBARE_TAGS:
                continue
            href = el.get_attribute("href") or ""
            if any(s in href for s in SKIP_HREF):
                continue
            driver.execute_script("arguments[0].click();", el)
            return True
    except Exception as e:
        log.warning("    -> Fout bij klikken op '%s': %s", naam, e)
    return False


def scrape_configurator_prijzen(driver, model_naam, model_url):
    """
    Volgt de 'Stel zelf samen' link van de modelpagina naar de configurator
    en haalt per brandstoftype de vanafprijs op.

    Fallback: als geen 'Stel zelf samen' gevonden → 'Bekijk voorraad' →
    goedkoopste elektrisch en overig uit de voorraadlijst.

    Geeft dict terug: {'Elektrisch': '€ 469,-', 'Overig': '€ 395,-'}
    """
    prijzen = {}

    try:
        # Stap 1: modelpagina laden
        driver.get(model_url)
        time.sleep(5)
        log.info("  -> Modelpagina: %s", model_url)

        configurator_url = vind_configurator_link(driver)

        # Fallback: Stellantis patroon /configurator/{model}/steps
        if not configurator_url:
            basis = re.match(r"(https?://[^/]+)", model_url)
            if basis:
                configurator_url = f"{basis.group(1)}/configurator/{model_naam}/steps"
                log.info("  -> Geen configurator-link, probeer: %s", configurator_url)

        # Als ook geconstrueerde URL niet werkt → probeer voorraad
        if not configurator_url:
            log.warning("  -> %s: geen configurator beschikbaar, probeer voorraad", model_naam)
            voorraad_url = vind_voorraad_link(driver)
            if voorraad_url:
                driver.get(voorraad_url)
                return scrape_voorraad_prijzen(driver, model_naam)
            return prijzen

        # Stap 2: configurator laden
        driver.get(configurator_url)
        time.sleep(8)
        log.info("  -> Configurator: %s", driver.current_url)

        snippet = driver.find_element(By.TAG_NAME, "body").text[:300].replace("\n", " ")
        log.info("  -> Snippet: %s", snippet)

        begin_prijs = haal_prijs_uit_pagina(driver)
        log.info("  -> Beginprijs: %s", begin_prijs)

        opties_namen = vind_brandstof_opties(driver)
        log.info("  -> Opties: %s", opties_namen)

        if not opties_namen:
            if begin_prijs:
                prijzen["Overig"] = begin_prijs
            elif not begin_prijs:
                log.info("  -> %s: geen opties of prijs in configurator, probeer voorraad", model_naam)
                driver.get(model_url)
                time.sleep(4)
                voorraad_url = vind_voorraad_link(driver)
                if voorraad_url:
                    driver.get(voorraad_url)
                    return scrape_voorraad_prijzen(driver, model_naam)
            return prijzen

        # Stap 3: per optie klikken (element opnieuw zoeken = geen stale reference)
        elektrisch_prijzen = []
        overig_prijzen = []

        for opt_naam in opties_namen:
            geklikt = klik_brandstof_optie(driver, opt_naam)
            if not geklikt:
                log.warning("    -> '%s': kon niet klikken", opt_naam)
                continue
            time.sleep(4)

            prijs = haal_prijs_uit_pagina(driver)
            if not prijs:
                log.warning("    -> '%s': geen prijs na klik", opt_naam)
                continue

            log.info("    -> '%s': %s", opt_naam, prijs)

            eerste_woord = opt_naam.lower().split()[0]
            if eerste_woord in ELEKTRISCH_TERMEN:
                elektrisch_prijzen.append(prijs)
            else:
                overig_prijzen.append(prijs)

        # Stap 4: resultaten samenstellen
        if elektrisch_prijzen:
            prijzen["Elektrisch"] = min(elektrisch_prijzen, key=prijs_naar_float)
        if overig_prijzen:
            prijzen["Overig"] = min(overig_prijzen, key=prijs_naar_float)
        elif begin_prijs and not elektrisch_prijzen:
            prijzen["Overig"] = begin_prijs

        # Als configurator helemaal geen resultaat gaf → voorraad als laatste kans
        if not prijzen:
            log.info("  -> %s: configurator leeg, probeer voorraad", model_naam)
            driver.get(model_url)
            time.sleep(4)
            voorraad_url = vind_voorraad_link(driver)
            if voorraad_url:
                driver.get(voorraad_url)
                prijzen = scrape_voorraad_prijzen(driver, model_naam)

    except Exception as e:
        log.error("  -> Fout bij configurator %s: %s", model_naam, e)

    return prijzen

# ─────────────────────────────────────────────
# HOOFD SCRAPER
# ─────────────────────────────────────────────

def scrape_merk(driver, merk):
    log.info("=" * 50)
    log.info("Scrapen: %s", merk["naam"])

    # Standaard merken: vanafprijzen direct van de overzichtspagina
    if merk["naam"] not in CONFIGURATOR_MERKEN:
        return scrape_overzicht_prijzen(driver, merk)

    # Alfa Romeo & Jeep: per model via configurator/voorraad
    prijzen = {}

    try:
        driver.get(merk["url"])
        time.sleep(5)
        model_urls = vind_model_links(driver, merk["url"])
        log.info("  -> %d modellen: %s", len(model_urls), list(model_urls.keys()))

        for model_naam, model_url in model_urls.items():
            config_prijzen = scrape_configurator_prijzen(driver, model_naam, model_url)
            if config_prijzen:
                for aandrijving, prijs in config_prijzen.items():
                    prijzen[f"{model_naam} ({aandrijving})"] = prijs
            else:
                log.warning("  -> %s: geen resultaat", model_naam)
            time.sleep(2)

    except Exception as e:
        log.error("  -> Fout bij %s: %s", merk["naam"], e)

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
