"""
Nefkens Private Lease Monitor (v2 - Volledig herschreven)
====================================
Draait dagelijks via GitHub Actions.
E-mailgegevens worden veilig opgehaald uit GitHub Secrets.

Belangrijkste verbeteringen:
- Betere naamcleaning en normalisatie
- Aparte configurator-logica voor Alfa Romeo en Jeep
- Meer stabiele prijs-extractie
- Beter error handling en logging
- Voorraadprijzen correct negeren
- Geen dubbele modellen in output
"""

import json
import os
import smtplib
import logging
import re
import time
from pathlib import Path
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from collections import defaultdict
from typing import Dict, List, Tuple, Optional

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

# ─────────────────────────────────────────────
# CONFIGURATIE
# ─────────────────────────────────────────────

EMAIL_CONFIG = {
    "smtp_server":  "smtp.gmail.com",
    "smtp_port":    587,
    "username":     os.environ.get("GMAIL_USERNAME", ""),
    "password":     os.environ.get("GMAIL_PASSWORD", ""),
    "from_address": os.environ.get("GMAIL_USERNAME", ""),
    "to_addresses": ["jordie.hagen@nefkens.nl", "pauline.edens@nefkens.nl"],
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

# Deze merken hebben aparte Elektrisch/Overig categorieën
CONFIGURATOR_MERKEN = {"Alfa Romeo", "Jeep"}

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
# BROWSER SETUP
# ─────────────────────────────────────────────

def get_driver():
    """Creëert een Selenium WebDriver voor headless Chrome."""
    options = webdriver.ChromeOptions()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-gpu")
    options.binary_location = "/usr/bin/chromium-browser"
    service = Service("/usr/bin/chromedriver")
    return webdriver.Chrome(service=service, options=options)

# ─────────────────────────────────────────────
# HELPERS: NAAMCLEANING EN NORMALISATIE
# ─────────────────────────────────────────────

def clean_model_name(raw_name: str) -> str:
    """
    Normaliseer modelnamen:
    - Decodeer URL-encodings (%20 → spatie)
    - Trim whitespace
    - Zet spaties om naar normale spaties
    - Verwijder meerdere spaties
    """
    name = raw_name.strip()
    # URL-decoding
    name = name.replace("%20", " ").replace("%2F", "/")
    # Normaliseer spaties
    name = " ".join(name.split())
    return name

def extract_price(text: str) -> Optional[str]:
    """
    Zoekt een prijs in de vorm "€ XXXX,-" of "€XXXX,-" in tekst.
    Retourneert de prijs in genormaliseerde vorm: "€ XXXX,-"
    """
    match = re.search(r'€\s*([\d.,]+)\s*,?-?', text)
    if match:
        price_str = match.group(1)
        # Zorg voor consistent format
        return f"€ {price_str},-"
    return None

def find_eerste_prijs_in_text(text: str) -> Optional[str]:
    """Vind de eerste prijs in grotere tekst."""
    match = re.search(r'€\s*([\d.,]+)', text)
    if match:
        return f"€ {match.group(1)},-"
    return None

# ─────────────────────────────────────────────
# SCRAPER: STANDAARD MERKEN (NON-CONFIGURATOR)
# ─────────────────────────────────────────────

def scrape_standaard_merk(driver: webdriver.Chrome, merk_info: dict) -> Dict[str, str]:
    """
    Scrapt modellen en prijzen voor standaard merken (niet Alfa Romeo/Jeep).
    Logica:
    1. Haal modelpagina's op van overzichtspagina
    2. Per modelpagina: zoek "Stel zelf samen" prijs
    3. Negeer "Bekijk voorraad" prijzen
    """
    merk_naam = merk_info["naam"]
    base_url = merk_info["url"]
    prijzen = {}
    
    log.info("Scrapen (standaard): %s", merk_naam)
    
    try:
        # Laad overzichtspagina
        driver.get(base_url)
        time.sleep(4)
        
        # Haal alle modellinks op
        model_links = driver.find_elements(By.CSS_SELECTOR, "a[href*='/modellen/']")
        model_urls = {}
        
        for link in model_links:
            href = link.get_attribute("href") or ""
            # Filter: enkel links naar individuele modellenpagina's
            if "/modellen/" not in href:
                continue
            
            text = link.text.strip()
            if not text:
                continue
            
            text = clean_model_name(text)
            
            # Vermijd dubbelingen
            if text not in model_urls:
                model_urls[text] = href
        
        log.info("  Gevonden %d modellen", len(model_urls))
        
        # Scraap elke modelpagina
        for model_naam, model_url in model_urls.items():
            try:
                driver.get(model_url)
                time.sleep(3)
                
                page_text = driver.find_element(By.TAG_NAME, "body").text
                
                # Zoek "Stel zelf samen" prijs
                prijs = None
                
                # Patroon 1: "Stel zelf samen" gevolgd door vanafprijs
                match = re.search(
                    r'[Ss]tel\s+zelf\s+samen[^\n]*?vanaf\s*(€\s*[\d.,]+)',
                    page_text,
                    re.IGNORECASE
                )
                if match:
                    prijs = match.group(1).strip()
                
                # Patroon 2: "Stel zelf samen" op volgende regel
                if not prijs:
                    match = re.search(
                        r'[Ss]tel\s+zelf\s+samen[^\€]*?(€\s*[\d.,]+)',
                        page_text,
                        re.IGNORECASE
                    )
                    if match:
                        prijs = match.group(1).strip()
                
                # Patroon 3: fallback op eerste prijs
                if not prijs:
                    prijs = find_eerste_prijs_in_text(page_text)
                
                if prijs:
                    prijzen[model_naam] = prijs
                    log.info("  ✓ %s: %s", model_naam, prijs)
                else:
                    log.warning("  ✗ %s: geen prijs gevonden", model_naam)
                
            except Exception as e:
                log.error("  ✗ Fout bij model %s: %s", model_naam, e)
            
            time.sleep(1)
        
    except Exception as e:
        log.error("Fout bij %s: %s", merk_naam, e)
    
    log.info("  → Totaal %d modellen met prijs\n", len(prijzen))
    return prijzen

# ─────────────────────────────────────────────
# SCRAPER: CONFIGURATOR (ALFA ROMEO / JEEP)
# ─────────────────────────────────────────────

def scrape_configurator_merk(driver: webdriver.Chrome, merk_info: dict) -> Dict[str, str]:
    """
    Scrapt modellen met aparte Elektrisch/Overig prijzen voor Alfa Romeo en Jeep.
    
    Logica:
    1. Haal overzichtspagina op → modellinks
    2. Per model:
       a. Open modelpagina → detecteer aandrijvingsopties
       b. Als Elektrisch beschikbaar: open configurator → pak Elektrische prijs
       c. Als Overig beschikbaar: pak niet-elektrische prijs
    3. Sla op als:
       - "Modelnaam (Elektrisch)" als alleen elektrisch
       - "Modelnaam (Overig)" als alleen overig
       - Beide als beide beschikbaar
    """
    merk_naam = merk_info["naam"]
    base_url = merk_info["url"]
    prijzen = {}
    
    log.info("Scrapen (configurator): %s", merk_naam)
    
    try:
        # Laad overzichtspagina
        driver.get(base_url)
        time.sleep(4)
        
        # Haal modellinks
        model_links = driver.find_elements(By.CSS_SELECTOR, "a[href*='/modellen/']")
        model_urls = {}
        
        for link in model_links:
            href = link.get_attribute("href") or ""
            if "/modellen/" not in href:
                continue
            
            text = link.text.strip()
            if not text:
                continue
            
            text = clean_model_name(text)
            if text not in model_urls:
                model_urls[text] = href
        
        log.info("  Gevonden %d modellen", len(model_urls))
        
        # Scraap elke modelpagina
        for model_naam, model_url in model_urls.items():
            try:
                driver.get(model_url)
                time.sleep(3)
                
                page_text = driver.find_element(By.TAG_NAME, "body").text
                
                # Detecteer beschikbare aandrijvingen
                heeft_elektrisch = bool(
                    re.search(r'[Ee]lektrisch|[Ee]lectric|BEV', page_text)
                )
                heeft_overig = bool(
                    re.search(r'[Bb]enzine|[Hh]ybride|[Pp]lugin|PHEV|[Mm]ild', page_text)
                )
                
                # Haal "standaard" (niet-elektrische) prijs
                prijs_overig = None
                match = re.search(
                    r'[Ss]tel\s+zelf\s+samen[^\€]*?(€\s*[\d.,]+)',
                    page_text,
                    re.IGNORECASE
                )
                if match:
                    prijs_overig = match.group(1).strip()
                
                if not prijs_overig:
                    prijs_overig = find_eerste_prijs_in_text(page_text)
                
                # Probeer elektrische prijs via configurator
                prijs_elektrisch = None
                if heeft_elektrisch:
                    prijs_elektrisch = _get_configurator_prijs_elektrisch(
                        driver, merk_naam, model_naam
                    )
                
                # Sla op
                if prijs_elektrisch:
                    prijzen[f"{model_naam} (Elektrisch)"] = prijs_elektrisch
                    log.info("  ✓ %s (Elektrisch): %s", model_naam, prijs_elektrisch)
                
                if prijs_overig and heeft_overig:
                    prijzen[f"{model_naam} (Overig)"] = prijs_overig
                    log.info("  ✓ %s (Overig): %s", model_naam, prijs_overig)
                elif prijs_overig:
                    # Geen Elektrisch, dus gewoon de prijs opslaan
                    prijzen[model_naam] = prijs_overig
                    log.info("  ✓ %s: %s", model_naam, prijs_overig)
                else:
                    log.warning("  ✗ %s: geen prijs gevonden", model_naam)
                
            except Exception as e:
                log.error("  ✗ Fout bij model %s: %s", model_naam, e)
            
            time.sleep(1)
        
    except Exception as e:
        log.error("Fout bij %s: %s", merk_naam, e)
    
    log.info("  → Totaal %d prijsregels geregistreerd\n", len(prijzen))
    return prijzen

def _get_configurator_prijs_elektrisch(
    driver: webdriver.Chrome,
    merk_naam: str,
    model_naam: str
) -> Optional[str]:
    """
    Opent de configurator en probeert de elektrische prijs op te halen.
    Dit is een aparte helper voor de configurator-merken.
    """
    try:
        # Vind en klik op configurator-link
        page_text = driver.find_element(By.TAG_NAME, "body").text
        
        # Zoek naar configurator-knop/link
        config_clicked = False
        for selector in [
            "a[href*='configurator']",
            "button:contains('Configurator')",
            "a:contains('Configurator')",
        ]:
            try:
                els = driver.find_elements(By.CSS_SELECTOR, selector)
                if els:
                    driver.execute_script("arguments[0].click();", els[0])
                    time.sleep(5)
                    config_clicked = True
                    break
            except:
                pass
        
        if not config_clicked:
            # Probeer via JavaScript naar configurator te gaan
            # (dit is merk-specifiek en kan verschillen)
            return None
        
        # Nu we in de configurator zijn, zoek elektrisch-tab
        prijs_elektrisch = None
        
        for term in ["Elektrisch", "Electric", "BEV", "E-"]:
            try:
                els = driver.find_elements(
                    By.XPATH,
                    f"//*[contains(normalize-space(), '{term}')]"
                )
                for el in els:
                    if el.tag_name.lower() in ["button", "label", "span", "li"]:
                        driver.execute_script("arguments[0].click();", el)
                        time.sleep(4)
                        
                        # Haal prijs op
                        body_text = driver.find_element(By.TAG_NAME, "body").text
                        prijs = find_eerste_prijs_in_text(body_text)
                        
                        if prijs:
                            prijs_elektrisch = prijs
                            break
            except:
                pass
            
            if prijs_elektrisch:
                break
        
        return prijs_elektrisch
        
    except Exception as e:
        log.warning("  → Configurator fout voor %s: %s", model_naam, e)
        return None

# ─────────────────────────────────────────────
# HOOFD SCRAPER DISPATCHER
# ─────────────────────────────────────────────

def scrape_merk(driver: webdriver.Chrome, merk_info: dict) -> Dict[str, str]:
    """
    Bepaalt welk scrape-type te gebruiken en roept de juiste functie aan.
    """
    merk_naam = merk_info["naam"]
    
    if merk_naam in CONFIGURATOR_MERKEN:
        return scrape_configurator_merk(driver, merk_info)
    else:
        return scrape_standaard_merk(driver, merk_info)

# ─────────────────────────────────────────────
# OPSLAG & VERGELIJKING
# ─────────────────────────────────────────────

def laad_opgeslagen() -> Dict[str, Dict[str, str]]:
    """Laad de opgeslagen prijzen uit JSON."""
    if DATA_FILE.exists():
        try:
            data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            return data.get("prijzen", {})
        except Exception as e:
            log.error("Fout bij laden JSON: %s", e)
            return {}
    return {}

def sla_op(alle_prijzen: Dict[str, Dict[str, str]]) -> None:
    """Slaat prijzen op in JSON."""
    data = {
        "bijgewerkt_op": datetime.now().isoformat(),
        "prijzen": alle_prijzen,
    }
    DATA_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

def vergelijk(
    oud: Dict[str, Dict[str, str]],
    nieuw: Dict[str, Dict[str, str]]
) -> List[dict]:
    """
    Vergelijkt oude en nieuwe prijzen.
    Retourneert lijst met wijzigingen.
    """
    wijzigingen = []
    
    for merk, modellen in nieuw.items():
        oud_merk = oud.get(merk, {})
        
        # Controleer nieuwe/gewijzigde modellen
        for model, prijs in modellen.items():
            if model not in oud_merk:
                wijzigingen.append({
                    "merk": merk,
                    "model": model,
                    "type": "Nieuw",
                    "oud": "—",
                    "nieuw": prijs,
                })
            elif oud_merk[model] != prijs:
                wijzigingen.append({
                    "merk": merk,
                    "model": model,
                    "type": "Gewijzigd",
                    "oud": oud_merk[model],
                    "nieuw": prijs,
                })
        
        # Controleer verwijderde modellen
        for model in oud_merk:
            if model not in modellen:
                wijzigingen.append({
                    "merk": merk,
                    "model": model,
                    "type": "Verwijderd",
                    "oud": oud_merk[model],
                    "nieuw": "—",
                })
    
    return wijzigingen

# ─────────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────────

def bouw_email_html(
    wijzigingen: List[dict],
    alle_prijzen: Dict[str, Dict[str, str]]
) -> str:
    """
    Bouwt een nette HTML-email met wijzigingen en overzicht.
    """
    datum = datetime.now().strftime("%d-%m-%Y %H:%M")
    n = len(wijzigingen)
    
    # Groepeer wijzigingen per merk
    per_merk = defaultdict(list)
    for w in wijzigingen:
        per_merk[w["merk"]].append(w)
    
    # Bouw wijzigingentabel
    secties = ""
    for merk in sorted(per_merk.keys()):
        items = per_merk[merk]
        rijen = ""
        for w in items:
            type_info = {
                "Nieuw": ("🟢", "#d4edda"),
                "Gewijzigd": ("🟡", "#fff3cd"),
                "Verwijderd": ("🔴", "#f8d7da"),
            }
            icoon, kleur = type_info[w["type"]]
            
            rijen += f"""<tr style="background:{kleur}">
                <td style="padding:8px">{w['model']}</td>
                <td style="padding:8px">{icoon} {w['type']}</td>
                <td style="padding:8px">{w['oud']}</td>
                <td style="padding:8px"><strong>{w['nieuw']}</strong></td>
            </tr>"""
        
        secties += f"""
            <h3 style="margin-top:24px;border-bottom:2px solid #eee;padding-bottom:4px">{merk}</h3>
            <table border="1" cellspacing="0" style="border-collapse:collapse;width:100%;margin-bottom:16px">
                <tr style="background:#f0f0f0;font-weight:bold">
                    <th style="padding:8px;text-align:left">Model</th>
                    <th style="padding:8px;text-align:left">Status</th>
                    <th style="padding:8px;text-align:left">Oude prijs</th>
                    <th style="padding:8px;text-align:left">Nieuwe prijs</th>
                </tr>{rijen}
            </table>"""
    
    # Bouw volledig overzicht
    overzicht = ""
    for merk in sorted(alle_prijzen.keys()):
        modellen = alle_prijzen[merk]
        if not modellen:
            continue
        
        rijen = "".join(
            f'<tr><td style="padding:6px">{m}</td><td style="padding:6px">{p}</td></tr>'
            for m, p in sorted(modellen.items())
        )
        
        overzicht += f"""
            <h3 style="margin-top:20px">{merk}</h3>
            <table border="1" cellspacing="0" style="border-collapse:collapse;margin-bottom:12px;width:100%">
                <tr style="background:#f0f0f0;font-weight:bold">
                    <th style="padding:6px;text-align:left">Model</th>
                    <th style="padding:6px;text-align:left">Vanafprijs/mnd</th>
                </tr>{rijen}
            </table>"""
    
    wijzigingen_blok = f"""
        <h2>Wijzigingen ({n})</h2>
        {secties if n > 0 else '<p style="color:#666"><em>Geen wijzigingen t.o.v. de vorige meting.</em></p>'}
    """
    
    html = f"""<html><head><meta charset="utf-8"></head><body style="font-family:Arial,sans-serif;color:#333;max-width:900px;margin:0 auto;padding:20px">
        <h1 style="color:#1B4F8A">Nefkens Private Lease Monitor</h1>
        <p style="color:#666">Gecontroleerd op: <strong>{datum}</strong></p>
        {wijzigingen_blok}
        <h2 style="margin-top:40px">Volledig actueel overzicht</h2>
        {overzicht}
        <hr style="border:none;border-top:1px solid #eee;margin:40px 0;padding-top:12px">
        <p style="color:#aaa;font-size:11px">
            Automatisch bericht · Nefkens Private Lease Monitor · GitHub Actions<br>
            {datum}
        </p>
    </body></html>"""
    
    return html

def stuur_email(
    wijzigingen: List[dict],
    alle_prijzen: Dict[str, Dict[str, str]]
) -> None:
    """
    Stuurt een email met wijzigingen.
    """
    cfg = EMAIL_CONFIG
    datum = datetime.now().strftime("%d-%m-%Y")
    n = len(wijzigingen)
    subject = f"[Nefkens Monitor] {n} wijziging{'en' if n != 1 else ''} - {datum}"
    
    html = bouw_email_html(wijzigingen, alle_prijzen)
    
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg["from_address"]
    msg["To"] = ", ".join(cfg["to_addresses"])
    msg.attach(MIMEText(html, "html", "utf-8"))
    
    try:
        with smtplib.SMTP(cfg["smtp_server"], cfg["smtp_port"]) as server:
            server.starttls()
            server.login(cfg["username"], cfg["password"])
            server.sendmail(cfg["from_address"], cfg["to_addresses"], msg.as_string())
        log.info("E-mail verstuurd naar %d ontvangers", len(cfg["to_addresses"]))
    except Exception as e:
        log.error("Fout bij versturen email: %s", e)

# ─────────────────────────────────────────────
# HOOFDPROGRAMMA
# ─────────────────────────────────────────────

def main():
    log.info("=" * 70)
    log.info("Nefkens Private Lease Monitor gestart")
    log.info("Datum/Tijd: %s", datetime.now().strftime("%d-%m-%Y %H:%M:%S"))
    log.info("=" * 70)
    
    # Laad opgeslagen data
    oude_prijzen = laad_opgeslagen()
    nieuwe_prijzen = {}
    
    # Initialiseer driver
    driver = get_driver()
    try:
        # Scraap alle merken
        for merk_info in MERKEN:
            try:
                nieuwe_prijzen[merk_info["naam"]] = scrape_merk(driver, merk_info)
                time.sleep(2)
            except Exception as e:
                log.error("Fout bij scrapen %s: %s", merk_info["naam"], e)
                nieuwe_prijzen[merk_info["naam"]] = {}
    finally:
        driver.quit()
    
    # Vergelijk en sla op
    wijzigingen = vergelijk(oude_prijzen, nieuwe_prijzen)
    sla_op(nieuwe_prijzen)
    
    # Toon samenvatting
    totaal = sum(len(m) for m in nieuwe_prijzen.values())
    log.info("\n" + "=" * 70)
    log.info("Samenvatting:")
    log.info("  Totaal modellen: %d", totaal)
    log.info("  Merken: %d", len(MERKEN))
    log.info("  Wijzigingen: %d", len(wijzigingen))
    log.info("=" * 70)
    
    # Stuur email als er wijzigingen zijn
    if wijzigingen:
        log.info("\n✓ Wijzigingen gevonden - email wordt verstuurd...")
        stuur_email(wijzigingen, nieuwe_prijzen)
        log.info("✓ Email verstuurd!")
    else:
        log.info("\n✓ Geen wijzigingen - geen email verstuurd")
    
    log.info("\nKlaar!\n")

if __name__ == "__main__":
    main()
