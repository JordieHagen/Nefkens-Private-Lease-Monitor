"""
Nefkens Private Lease Monitor
==============================
Monitorert prijzen van private lease auto's van 10 Nederlandse merkenen.

LOGICA:
- Standaard merken (8): Haal prijzen van overzichtspagina's (/modellen)
- Configurator merken (2): Open modelpagina → configurator → stel zelf samen → (E) en (O)
                            Of: bekijk voorraad → goedkoopste (E) en (O)
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
# CONFIG
# ─────────────────────────────────────────────

EMAIL_CONFIG = {
    "smtp_server": "smtp.gmail.com",
    "smtp_port": 587,
    "username": os.environ.get("GMAIL_USERNAME", ""),
    "password": os.environ.get("GMAIL_PASSWORD", ""),
    "from_address": os.environ.get("GMAIL_USERNAME", ""),
    "to_addresses": ["jordie.hagen@nefkens.nl", "pauline.edens@nefkens.nl"],
}

MERKEN_STANDAARD = [
    {"naam": "Peugeot", "url": "https://privatelease.peugeot.nl/modellen"},
    {"naam": "Citroën", "url": "https://privatelease.citroen.nl/modellen"},
    {"naam": "DS Automobiles", "url": "https://privatelease.dsautomobiles.nl/modellen"},
    {"naam": "Opel", "url": "https://privatelease.opel.nl/modellen"},
    {"naam": "Fiat", "url": "https://privatelease.fiat.nl/modellen"},
    {"naam": "Abarth", "url": "https://privatelease.abarth.nl/modellen"},
    {"naam": "Lancia", "url": "https://privatelease.lancia.nl/modellen"},
    {"naam": "Leapmotor", "url": "https://privatelease.leapmotor.nl/modellen"},
]

MERKEN_CONFIGURATOR = [
    {"naam": "Alfa Romeo", "url": "https://privatelease.alfaromeo.nl/modellen"},
    {"naam": "Jeep", "url": "https://privatelease.jeep.nl/modellen"},
]

DATA_FILE = Path("nefkens_prices.json")
LOG_FILE = Path("nefkens_monitor.log")

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-10s  %(message)s",
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

def clean_model_name(text: str) -> str:
    """Cleanup model naam."""
    if not text:
        return ""
    text = text.strip()
    text = text.replace("%20", " ").replace("%2F", "/")
    text = " ".join(text.split())
    return text

def extract_price(text: str) -> Optional[str]:
    """Extract prijs uit tekst. Retourneert €XXX,- of None."""
    if not text:
        return None
    # Zoek getal 200-999 (redelijke lease prijs)
    match = re.search(r'\b([2-9]\d{2})\b', str(text))
    if match:
        getal = int(match.group(1))
        if 200 <= getal <= 999:
            return f"€ {getal},-"
    return None

# ─────────────────────────────────────────────
# STANDAARD MERKEN: OVERZICHTSPAGINA
# ─────────────────────────────────────────────

def scrape_standaard(driver: webdriver.Chrome, merk_info: dict) -> Dict[str, str]:
    """
    Standaard merken: Haal modelnamen + prijzen DIRECT van overzichtspagina.
    Niet van modelpagina's!
    """
    merk_naam = merk_info["naam"]
    base_url = merk_info["url"]
    prijzen = {}
    
    log.info("SCRAPEN: %s (overzicht)", merk_naam)
    
    try:
        driver.get(base_url)
        time.sleep(4)
        
        page_text = driver.find_element(By.TAG_NAME, "body").text
        
        # Vind alle modellinks
        model_links = driver.find_elements(By.CSS_SELECTOR, "a[href*='/modellen/']")
        model_names = []
        
        for link in model_links:
            href = link.get_attribute("href") or ""
            # Skip configurator links
            if "/configurator/" in href:
                continue
            
            text = link.text.strip()
            if text:
                text = clean_model_name(text)
                if text and text not in model_names:
                    model_names.append(text)
        
        log.info("  Gevonden %d modellen", len(model_names))
        
        # Per model: zoek prijs in overzicht
        for model_naam in model_names:
            try:
                # Vind model in pagina en zoek volgende prijs
                idx = page_text.upper().find(model_naam.upper())
                if idx < 0:
                    continue
                
                # Snippet van 200 chars na model naam
                snippet = page_text[idx:idx+200]
                
                # Extract prijs
                prijs = extract_price(snippet)
                
                if prijs:
                    prijzen[model_naam] = prijs
                    log.info("  ✓ %s: %s", model_naam, prijs)
                else:
                    log.warning("  ✗ %s: geen prijs", model_naam)
            
            except Exception as e:
                log.error("  ✗ Fout %s: %s", model_naam, e)
        
    except Exception as e:
        log.error("Fout %s: %s", merk_naam, e)
    
    log.info("  → %d modellen opgeslagen\n", len(prijzen))
    return prijzen

# ─────────────────────────────────────────────
# CONFIGURATOR MERKEN: MODELPAGINA
# ─────────────────────────────────────────────

def scrape_configurator(driver: webdriver.Chrome, merk_info: dict) -> Dict[str, str]:
    """
    Alfa Romeo & Jeep: Open modelpagina → zoek configurator of bekijk voorraad.
    
    Stap 1: Probeer "Stel zelf samen" button
    Stap 2: Fallback: "Bekijk voorraad" button
    """
    merk_naam = merk_info["naam"]
    base_url = merk_info["url"]
    prijzen = {}
    
    log.info("SCRAPEN: %s (configurator)", merk_naam)
    
    try:
        driver.get(base_url)
        time.sleep(4)
        
        # Vind modellen
        model_links = driver.find_elements(By.CSS_SELECTOR, "a[href*='/modellen/']")
        model_names = []
        model_urls = {}
        
        for link in model_links:
            href = link.get_attribute("href") or ""
            if "/modellen/" not in href or "/configurator/" in href:
                continue
            
            text = link.text.strip()
            if text:
                text = clean_model_name(text)
                if text not in model_names:
                    model_names.append(text)
                    model_urls[text] = href
        
        log.info("  Gevonden %d modellen", len(model_names))
        
        # Per model
        for model_naam, model_url in model_urls.items():
            try:
                driver.get(model_url)
                time.sleep(3)
                
                page_text = driver.find_element(By.TAG_NAME, "body").text
                
                # Stap 1: Zoek "Stel zelf samen" button
                prijs_elektrisch = None
                prijs_overig = None
                
                try:
                    config_buttons = driver.find_elements(
                        By.XPATH,
                        "//a[contains(., 'Stel zelf samen')] | //button[contains(., 'Stel zelf samen')]"
                    )
                    
                    if config_buttons:
                        log.info("  → %s: Found 'Stel zelf samen'", model_naam)
                        prijs_elektrisch, prijs_overig = _scrape_stel_zelf_samen(driver, model_url)
                except Exception as e:
                    log.debug("  → Geen 'Stel zelf samen': %s", e)
                
                # Stap 2: Fallback naar "Bekijk voorraad"
                if not prijs_overig:
                    try:
                        voorraad_buttons = driver.find_elements(
                            By.XPATH,
                            "//a[contains(., 'Bekijk voorraad')] | //button[contains(., 'Bekijk voorraad')]"
                        )
                        
                        if voorraad_buttons:
                            log.info("  → %s: Fallback 'Bekijk voorraad'", model_naam)
                            prijs_elektrisch, prijs_overig = _scrape_bekijk_voorraad(driver, voorraad_buttons[0])
                    except Exception as e:
                        log.debug("  → Geen 'Bekijk voorraad': %s", e)
                
                # Sla op
                if prijs_overig:
                    if prijs_elektrisch:
                        prijzen[f"{model_naam} (Elektrisch)"] = prijs_elektrisch
                        prijzen[f"{model_naam} (Overig)"] = prijs_overig
                        log.info("  ✓ %s (Elektrisch): %s", model_naam, prijs_elektrisch)
                        log.info("  ✓ %s (Overig): %s", model_naam, prijs_overig)
                    else:
                        prijzen[model_naam] = prijs_overig
                        log.info("  ✓ %s: %s", model_naam, prijs_overig)
                else:
                    log.warning("  ✗ %s: geen prijzen gevonden", model_naam)
            
            except Exception as e:
                log.error("  ✗ Fout %s: %s", model_naam, e)
            
            time.sleep(1)
    
    except Exception as e:
        log.error("Fout %s: %s", merk_naam, e)
    
    log.info("  → %d prijsregels\n", len(prijzen))
    return prijzen

def _scrape_stel_zelf_samen(driver: webdriver.Chrome, config_url: str) -> tuple:
    """
    Open 'Stel zelf samen' configurator.
    Haal Overig prijs en probeer Elektrisch tab.
    """
    prijs_elektrisch = None
    prijs_overig = None
    
    try:
        # Open configurator
        driver.get(config_url)
        time.sleep(5)
        
        config_text = driver.find_element(By.TAG_NAME, "body").text
        
        # Haal Overig prijs
        prijs_overig = extract_price(config_text)
        
        if prijs_overig:
            log.info("      ✓ Overig: %s", prijs_overig)
        
        # Probeer Elektrisch tab
        for term in ["Elektrisch", "Electric", "BEV", "E-"]:
            try:
                els = driver.find_elements(By.XPATH, f"//*[contains(normalize-space(), '{term}')]")
                
                for el in els:
                    if el.is_displayed():
                        driver.execute_script("arguments[0].click();", el)
                        time.sleep(4)
                        
                        config_text = driver.find_element(By.TAG_NAME, "body").text
                        prijs_e = extract_price(config_text)
                        
                        if prijs_e and prijs_e != prijs_overig:
                            prijs_elektrisch = prijs_e
                            log.info("      ✓ Elektrisch: %s", prijs_elektrisch)
                            break
            
            except Exception as e:
                log.debug("      → Term '%s' error: %s", term, e)
            
            if prijs_elektrisch:
                break
    
    except Exception as e:
        log.error("      Fout stel zelf samen: %s", e)
    
    return prijs_elektrisch, prijs_overig

def _scrape_bekijk_voorraad(driver: webdriver.Chrome, button) -> tuple:
    """
    Open 'Bekijk voorraad' en haal goedkoopste Elektrisch + Overig.
    """
    prijs_elektrisch = None
    prijs_overig = None
    
    try:
        driver.execute_script("arguments[0].click();", button)
        time.sleep(5)
        
        voorraad_text = driver.find_element(By.TAG_NAME, "body").text
        
        # Zoek alle prijzen (200-999)
        prijzen = []
        for match in re.finditer(r'\b([2-9]\d{2})\b', voorraad_text):
            getal = int(match.group(1))
            if 200 <= getal <= 999:
                prijzen.append(getal)
        
        if prijzen:
            # Goedkoopste is eerste (of minimum)
            goedkoopste = min(prijzen)
            prijs_overig = f"€ {goedkoopste},-"
            log.info("      ✓ Overig (goedkoopste): %s", prijs_overig)
            
            # Check of Elektrisch anders is
            if len(prijzen) > 1 and prijzen[-1] != goedkoopste:
                prijs_elektrisch = f"€ {prijzen[-1]},-"
                log.info("      ✓ Elektrisch: %s", prijs_elektrisch)
    
    except Exception as e:
        log.error("      Fout bekijk voorraad: %s", e)
    
    return prijs_elektrisch, prijs_overig

# ─────────────────────────────────────────────
# OPSLAG & VERGELIJKING
# ─────────────────────────────────────────────

def laad_opgeslagen() -> Dict[str, Dict[str, str]]:
    """Laad opgeslagen prijzen."""
    if DATA_FILE.exists():
        try:
            data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            return data.get("prijzen", {})
        except Exception as e:
            log.error("Fout laden JSON: %s", e)
    return {}

def sla_op(alle_prijzen: Dict[str, Dict[str, str]]) -> None:
    """Sla prijzen op."""
    data = {
        "bijgewerkt_op": datetime.now().isoformat(),
        "prijzen": alle_prijzen,
    }
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def vergelijk(oud: Dict[str, Dict[str, str]], nieuw: Dict[str, Dict[str, str]]) -> List[dict]:
    """Vergelijk prijzen."""
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
    """Bouwt HTML email."""
    datum = datetime.now().strftime("%d-%m-%Y %H:%M")
    n = len(wijzigingen)
    
    per_merk = defaultdict(list)
    for w in wijzigingen:
        per_merk[w["merk"]].append(w)
    
    secties = ""
    for merk in sorted(per_merk.keys()):
        rijen = ""
        for w in per_merk[merk]:
            type_info = {
                "Nieuw": ("🟢", "#d4edda"),
                "Gewijzigd": ("🟡", "#fff3cd"),
                "Verwijderd": ("🔴", "#f8d7da"),
            }
            icoon, kleur = type_info[w["type"]]
            rijen += f'<tr style="background:{kleur}"><td style="padding:8px">{w["model"]}</td><td style="padding:8px">{icoon} {w["type"]}</td><td style="padding:8px">{w["oud"]}</td><td style="padding:8px"><strong>{w["nieuw"]}</strong></td></tr>'
        
        secties += f'<h3 style="margin-top:24px">{merk}</h3><table border="1" cellspacing="0" style="border-collapse:collapse;width:100%"><tr style="background:#f0f0f0"><th style="padding:8px">Model</th><th style="padding:8px">Status</th><th style="padding:8px">Oud</th><th style="padding:8px">Nieuw</th></tr>{rijen}</table>'
    
    html = f'<html><head><meta charset="utf-8"></head><body style="font-family:Arial;max-width:900px;margin:0 auto;padding:20px"><h1>Nefkels Monitor</h1><p>Gecontroleerd: {datum}</p><h2>Wijzigingen ({n})</h2>{secties if n > 0 else "<p>Geen wijzigingen.</p>"}</body></html>'
    return html

def stuur_email(wijzigingen: List[dict], alle_prijzen: Dict[str, Dict[str, str]]) -> None:
    """Stuurt email."""
    if not wijzigingen:
        log.info("Geen wijzigingen - email niet verstuurd")
        return
    
    cfg = EMAIL_CONFIG
    n = len(wijzigingen)
    subject = f"[Nefkels] {n} wijziging{'en' if n != 1 else ''}"
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
        log.info("✓ Email verstuurd")
    except Exception as e:
        log.error("Email error: %s", e)

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    log.info("=" * 70)
    log.info("Monitor gestart")
    log.info("=" * 70)
    
    oude_prijzen = laad_opgeslagen()
    nieuwe_prijzen = {}
    
    driver = get_driver()
    try:
        # Standaard merken
        for merk_info in MERKEN_STANDAARD:
            try:
                nieuwe_prijzen[merk_info["naam"]] = scrape_standaard(driver, merk_info)
                time.sleep(2)
            except Exception as e:
                log.error("Fout %s: %s", merk_info["naam"], e)
                nieuwe_prijzen[merk_info["naam"]] = {}
        
        # Configurator merken
        for merk_info in MERKEN_CONFIGURATOR:
            try:
                nieuwe_prijzen[merk_info["naam"]] = scrape_configurator(driver, merk_info)
                time.sleep(2)
            except Exception as e:
                log.error("Fout %s: %s", merk_info["naam"], e)
                nieuwe_prijzen[merk_info["naam"]] = {}
    
    finally:
        driver.quit()
    
    # Vergelijk en sla op
    wijzigingen = vergelijk(oude_prijzen, nieuwe_prijzen)
    sla_op(nieuwe_prijzen)
    
    totaal = sum(len(m) for m in nieuwe_prijzen.values())
    log.info("\n" + "=" * 70)
    log.info("Totaal: %d modellen, %d wijzigingen", totaal, len(wijzigingen))
    log.info("=" * 70)
    
    stuur_email(wijzigingen, nieuwe_prijzen)
    log.info("Klaar!\n")

if __name__ == "__main__":
    main()
