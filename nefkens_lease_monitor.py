"""
Nefkens Private Lease Monitor
====================================
Draait dagelijks via GitHub Actions.
E-mailgegevens worden veilig opgehaald uit GitHub Secrets.
"""

import json
import os
import smtplib
import logging
from pathlib import Path
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import time

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
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
# SCRAPER
# ─────────────────────────────────────────────

def scrape_merk(driver, merk):
    log.info("Scrapen: %s", merk["naam"])
    prijzen = {}
    try:
        driver.get(merk["url"])
        time.sleep(5)

        card_selectors = [
            ("[class*='model-card']",   "[class*='model-name'], h2, h3", "[class*='price'], [class*='vanaf'], [class*='amount']"),
            ("[class*='ModelCard']",    "[class*='name'], h2, h3",       "[class*='Price'], [class*='price']"),
            ("[class*='vehicle-card']", "h2, h3",                        "[class*='price']"),
            ("article",                 "h2, h3",                        "[class*='price'], [class*='vanaf']"),
        ]

        for card_sel, name_sel, price_sel in card_selectors:
            cards = driver.find_elements(By.CSS_SELECTOR, card_sel)
            if not cards:
                continue
            for card in cards:
                try:
                    name  = card.find_element(By.CSS_SELECTOR, name_sel).text.strip()
                    price = card.find_element(By.CSS_SELECTOR, price_sel).text.strip()
                    if name and price and ("€" in price or ",-" in price):
                        prijzen[name] = price
                except Exception:
                    continue
            if prijzen:
                log.info("  -> %d modellen gevonden", len(prijzen))
                break

        if not prijzen:
            log.warning("  -> Geen prijzen gevonden voor %s", merk["naam"])

    except Exception as e:
        log.error("  -> Fout bij %s: %s", merk["naam"], e)

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
        json.dumps({"bijgewerkt_op": datetime.now().isoformat(), "prijzen": alle_prijzen},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

def vergelijk(oud, nieuw):
    wijzigingen = []
    for merk, modellen in nieuw.items():
        oud_merk = oud.get(merk, {})
        for model, prijs in modellen.items():
            if model not in oud_merk:
                wijzigingen.append({"merk": merk, "model": model, "type": "Nieuw",      "oud": "—",            "nieuw": prijs})
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
    cfg    = EMAIL_CONFIG
    datum  = datetime.now().strftime("%d-%m-%Y")
    n      = len(wijzigingen)
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
    log.info("E-mail verstuurd")

# ─────────────────────────────────────────────
# HOOFDPROGRAMMA
# ─────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info("Nefkens Private Lease Monitor gestart - %s", datetime.now().strftime("%d-%m-%Y %H:%M"))
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
        log.info("%d wijziging(en) - e-mail wordt verstuurd", len(wijzigingen))
        stuur_email(wijzigingen, nieuwe_prijzen)
    else:
        log.info("Geen wijzigingen - geen e-mail verstuurd")

    log.info("Klaar.\n")

if __name__ == "__main__":
    main()
