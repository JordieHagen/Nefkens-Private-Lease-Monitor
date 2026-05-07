"""
Nefkens Private Lease Monitor (v3 - CORRECT LOGIC)
====================================
Draait dagelijks via GitHub Actions.
E-mailgegevens worden veilig opgehaald uit GitHub Secrets.

CRUCIALE LOGICA VERSCHIL:
- Standaard merken: Haal prijzen DIRECT uit overzicht (niet Stel zelf samen)
- Alfa Romeo/Jeep: Open configurator → haal Stel zelf samen prijzen → Elektrisch/Overig
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
from typing import Dict, List, Optional

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

CONFIGURATOR_MERKEN = {"Alfa Romeo", "Jeep"}

DATA_FILE = Path("nefkens_prices.json")
LOG_FILE  = Path("nefkens_monitor.log")

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.DEBUG if os.environ.get("DEBUG") else logging.INFO,
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
    options.add_argument("--disable-gpu")
    options.binary_location = "/usr/bin/chromium-browser"
    service = Service("/usr/bin/chromedriver")
    return webdriver.Chrome(service=service, options=options)

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def clean_model_name(raw_name: str) -> str:
    if not raw_name:
        return ""
    name = raw_name.strip()
    name = name.replace("%20", " ").replace("%2F", "/")
    name = " ".join(name.split())
    return name

# ─────────────────────────────────────────────
# SCRAPER: STANDAARD (OVERZICHT)
# ─────────────────────────────────────────────

def scrape_standaard_merk(driver: webdriver.Chrome, merk_info: dict) -> Dict[str, str]:
    """
    Standaard merken: Haal modelnamen EN PRIJZEN DIRECT UIT OVERZICHT.
    Niet van modelpagina's!
    """
    merk_naam = merk_info["naam"]
    base_url = merk_info["url"]
    prijzen = {}
    
    log.info("Scrapen (standaard - OVERZICHT): %s", merk_naam)
    
    try:
        driver.get(base_url)
        time.sleep(4)
        
        page_text = driver.find_element(By.TAG_NAME, "body").text
        
        # Vind modelnamen uit links
        model_links = driver.find_elements(By.CSS_SELECTOR, "a[href*='/modellen/']")
        model_names = []
        
        for link in model_links:
            href = link.get_attribute("href") or ""
            if "/configurator/" in href:
                continue
            
            text = link.text.strip()
            if not text:
                continue
            
            text = clean_model_name(text)
            if text and text not in model_names:
                model_names.append(text)
        
        log.info("  Gevonden %d modellen in links", len(model_names))
        
        # Per model naam → vind prijs in pagina
        for model_naam in model_names:
            try:
                safe_name = re.escape(model_naam)
                pattern = safe_name + r'[^\€]*?(€\s*[\d.,]+)'
                match = re.search(pattern, page_text, re.IGNORECASE)
                
                if match:
                    prijs = match.group(1).strip()
                    prijzen[model_naam] = prijs
                    log.info("  ✓ %s: %s", model_naam, prijs)
                else:
                    log.warning("  ✗ %s: geen prijs gevonden", model_naam)
                
            except Exception as e:
                log.error("  ✗ Fout bij %s: %s", model_naam, e)
        
    except Exception as e:
        log.error("Fout bij %s: %s", merk_naam, e)
    
    log.info("  → Totaal %d modellen met prijs\n", len(prijzen))
    return prijzen

# ─────────────────────────────────────────────
# SCRAPER: CONFIGURATOR (ALFA ROMEO / JEEP)
# ─────────────────────────────────────────────

def scrape_configurator_merk(driver: webdriver.Chrome, merk_info: dict) -> Dict[str, str]:
    """
    Alfa Romeo & Jeep: Open CONFIGURATOR voor Stel zelf samen prijzen.
    """
    merk_naam = merk_info["naam"]
    base_url = merk_info["url"]
    prijzen = {}
    
    log.info("Scrapen (configurator): %s", merk_naam)
    
    try:
        driver.get(base_url)
        time.sleep(4)
        
        model_links = driver.find_elements(By.CSS_SELECTOR, "a[href*='/modellen/']")
        modellen = []
        model_urls = {}
        
        for link in model_links:
            href = link.get_attribute("href") or ""
            if "/modellen/" not in href or "/configurator/" in href:
                continue
            
            text = link.text.strip()
            if text:
                text = clean_model_name(text)
                if text and text not in modellen:
                    modellen.append(text)
                    model_urls[text] = href
        
        log.info("  Gevonden %d modellen", len(modellen))
        
        for model_naam, model_url in model_urls.items():
            try:
                driver.get(model_url)
                time.sleep(3)
                
                page_text = driver.find_element(By.TAG_NAME, "body").text
                
                # Detecteer aandrijvingen
                heeft_elektrisch = bool(re.search(r'[Ee]lektrisch|[Ee]lectric|BEV', page_text))
                heeft_overig = bool(re.search(r'[Bb]enzine|[Hh]ybride|[Pp]lugin|PHEV', page_text))
                
                # Vind configurator link
                config_url = None
                try:
                    config_links = driver.find_elements(By.XPATH, "//a[contains(@href, 'configurator')]")
                    if config_links:
                        config_url = config_links[0].get_attribute("href")
                except:
                    pass
                
                if not config_url:
                    log.warning("  ✗ %s: geen configurator link", model_naam)
                    continue
                
                # Maak URL absoluut
                if config_url.startswith("/"):
                    domain = "/".join(driver.current_url.split("/")[:3])
                    config_url = domain + config_url
                
                log.info("  → %s: Open configurator...", model_naam)
                driver.get(config_url)
                time.sleep(5)
                
                # Haal "Stel zelf samen" prijs
                config_text = driver.find_element(By.TAG_NAME, "body").text
                match = re.search(
                    r'[Ss]tel\s+zelf\s+samen[^\€]*?(€\s*[\d.,]+)',
                    config_text,
                    re.IGNORECASE | re.DOTALL
                )
                prijs_overig = match.group(1).strip() if match else None
                
                if not prijs_overig:
                    match = re.search(r'€\s*([\d.,]+)', config_text)
                    prijs_overig = f"€ {match.group(1)},-" if match else None
                
                if prijs_overig:
                    log.info("    ✓ Basis (Overig): %s", prijs_overig)
                
                # Probeer Elektrisch
                prijs_elektrisch = None
                if heeft_elektrisch:
                    for term in ["Elektrisch", "Electric", "BEV"]:
                        try:
                            els = driver.find_elements(
                                By.XPATH,
                                f"//*[contains(normalize-space(), '{term}')]"
                            )
                            for el in els:
                                if el.is_displayed():
                                    driver.execute_script("arguments[0].click();", el)
                                    time.sleep(4)
                                    
                                    config_text = driver.find_element(By.TAG_NAME, "body").text
                                    match = re.search(
                                        r'[Ss]tel\s+zelf\s+samen[^\€]*?(€\s*[\d.,]+)',
                                        config_text,
                                        re.IGNORECASE | re.DOTALL
                                    )
                                    
                                    if match:
                                        prijs_elektrisch = match.group(1).strip()
                                    else:
                                        match = re.search(r'€\s*([\d.,]+)', config_text)
                                        prijs_elektrisch = f"€ {match.group(1)},-" if match else None
                                    
                                    if prijs_elektrisch:
                                        log.info("    ✓ Elektrisch: %s", prijs_elektrisch)
                                        break
                            if prijs_elektrisch:
                                break
                        except:
                            pass
                
                # Sla op
                if prijs_elektrisch:
                    prijzen[f"{model_naam} (Elektrisch)"] = prijs_elektrisch
                
                if prijs_overig:
                    if prijs_elektrisch:
                        prijzen[f"{model_naam} (Overig)"] = prijs_overig
                    else:
                        prijzen[model_naam] = prijs_overig
                else:
                    log.warning("  ✗ %s: geen prijzen gevonden", model_naam)
                
            except Exception as e:
                log.error("  ✗ Fout bij %s: %s", model_naam, e)
            
            time.sleep(1)
        
    except Exception as e:
        log.error("Fout bij %s: %s", merk_naam, e)
    
    log.info("  → Totaal %d prijsregels\n", len(prijzen))
    return prijzen

# ─────────────────────────────────────────────
# DISPATCHER
# ─────────────────────────────────────────────

def scrape_merk(driver: webdriver.Chrome, merk_info: dict) -> Dict[str, str]:
    if merk_info["naam"] in CONFIGURATOR_MERKEN:
        return scrape_configurator_merk(driver, merk_info)
    else:
        return scrape_standaard_merk(driver, merk_info)

# ─────────────────────────────────────────────
# OPSLAG & VERGELIJKING
# ─────────────────────────────────────────────

def laad_opgeslagen() -> Dict[str, Dict[str, str]]:
    if DATA_FILE.exists():
        try:
            data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            return data.get("prijzen", {})
        except Exception as e:
            log.error("Fout bij laden JSON: %s", e)
            return {}
    return {}

def sla_op(alle_prijzen: Dict[str, Dict[str, str]]) -> None:
    data = {
        "bijgewerkt_op": datetime.now().isoformat(),
        "prijzen": alle_prijzen,
    }
    DATA_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

def vergelijk(oud: Dict[str, Dict[str, str]], nieuw: Dict[str, Dict[str, str]]) -> List[dict]:
    wijzigingen = []
    
    for merk, modellen in nieuw.items():
        oud_merk = oud.get(merk, {})
        for model, prijs in modellen.items():
            if model not in oud_merk:
                wijzigingen.append({"merk": merk, "model": model, "type": "Nieuw", "oud": "—", "nieuw": prijs})
            elif oud_merk[model] != prijs:
                wijzigingen.append({"merk": merk, "model": model, "type": "Gewijzigd", "oud": oud_merk[model], "nieuw": prijs})
        for model in oud_merk:
            if model not in modellen:
                wijzigingen.append({"merk": merk, "model": model, "type": "Verwijderd", "oud": oud_merk[model], "nieuw": "—"})
    
    return wijzigingen

# ─────────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────────

def bouw_email_html(wijzigingen: List[dict], alle_prijzen: Dict[str, Dict[str, str]]) -> str:
    datum = datetime.now().strftime("%d-%m-%Y %H:%M")
    n = len(wijzigingen)
    
    per_merk = defaultdict(list)
    for w in wijzigingen:
        per_merk[w["merk"]].append(w)
    
    secties = ""
    for merk in sorted(per_merk.keys()):
        rijen = ""
        for w in per_merk[merk]:
            type_info = {"Nieuw": ("🟢", "#d4edda"), "Gewijzigd": ("🟡", "#fff3cd"), "Verwijderd": ("🔴", "#f8d7da")}
            icoon, kleur = type_info[w["type"]]
            rijen += f'<tr style="background:{kleur}"><td style="padding:8px">{w["model"]}</td><td style="padding:8px">{icoon} {w["type"]}</td><td style="padding:8px">{w["oud"]}</td><td style="padding:8px"><strong>{w["nieuw"]}</strong></td></tr>'
        
        secties += f'<h3 style="margin-top:24px;border-bottom:2px solid #eee;padding-bottom:4px">{merk}</h3><table border="1" cellspacing="0" style="border-collapse:collapse;width:100%;margin-bottom:16px"><tr style="background:#f0f0f0;font-weight:bold"><th style="padding:8px;text-align:left">Model</th><th style="padding:8px;text-align:left">Status</th><th style="padding:8px;text-align:left">Oude prijs</th><th style="padding:8px;text-align:left">Nieuwe prijs</th></tr>{rijen}</table>'
    
    overzicht = ""
    for merk in sorted(alle_prijzen.keys()):
        modellen = alle_prijzen[merk]
        if modellen:
            rijen = "".join(f'<tr><td style="padding:6px">{m}</td><td style="padding:6px">{p}</td></tr>' for m, p in sorted(modellen.items()))
            overzicht += f'<h3 style="margin-top:20px">{merk}</h3><table border="1" cellspacing="0" style="border-collapse:collapse;margin-bottom:12px;width:100%"><tr style="background:#f0f0f0;font-weight:bold"><th style="padding:6px;text-align:left">Model</th><th style="padding:6px;text-align:left">Vanafprijs/mnd</th></tr>{rijen}</table>'
    
    wijzigingen_blok = f'<h2>Wijzigingen ({n})</h2>{secties if n > 0 else "<p style=\"color:#666\"><em>Geen wijzigingen.</em></p>"}'
    
    return f'<html><head><meta charset="utf-8"></head><body style="font-family:Arial,sans-serif;color:#333;max-width:900px;margin:0 auto;padding:20px"><h1 style="color:#1B4F8A">Nefkels Monitor</h1><p>Gecontroleerd: <strong>{datum}</strong></p>{wijzigingen_blok}<h2 style="margin-top:40px">Overzicht</h2>{overzicht}</body></html>'

def stuur_email(wijzigingen: List[dict], alle_prijzen: Dict[str, Dict[str, str]]) -> None:
    cfg = EMAIL_CONFIG
    datum = datetime.now().strftime("%d-%m-%Y")
    n = len(wijzigingen)
    subject = f"[Nefkels Monitor] {n} wijziging{'en' if n != 1 else ''} - {datum}"
    
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
        log.info("✓ E-mail verstuurd")
    except Exception as e:
        log.error("Email error: %s", e)

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    log.info("=" * 70)
    log.info("Monitor v3 gestart - %s", datetime.now().strftime("%d-%m-%Y %H:%M"))
    log.info("=" * 70)
    
    oude_prijzen = laad_opgeslagen()
    nieuwe_prijzen = {}
    
    driver = get_driver()
    try:
        for merk_info in MERKEN:
            try:
                nieuwe_prijzen[merk_info["naam"]] = scrape_merk(driver, merk_info)
                time.sleep(2)
            except Exception as e:
                log.error("Fout bij %s: %s", merk_info["naam"], e)
                nieuwe_prijzen[merk_info["naam"]] = {}
    finally:
        driver.quit()
    
    wijzigingen = vergelijk(oude_prijzen, nieuwe_prijzen)
    sla_op(nieuwe_prijzen)
    
    totaal = sum(len(m) for m in nieuwe_prijzen.values())
    log.info("\n" + "=" * 70)
    log.info("Totaal: %d modellen, %d merken, %d wijzigingen", totaal, len(MERKEN), len(wijzigingen))
    log.info("=" * 70)
    
    if wijzigingen:
        log.info("✓ Wijzigingen - email verstuurd")
        stuur_email(wijzigingen, nieuwe_prijzen)
    else:
        log.info("✓ Geen wijzigingen")
    
    log.info("Klaar!\n")

if __name__ == "__main__":
    main()
