import requests
import urllib3
from bs4 import BeautifulSoup
import pandas as pd
import re
import os
import logging

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def fetch_data():
    url = "https://www.tjdft.jus.br/funcionamento/enderecos-e-telefones-old/lista-de-emails-das-varas-e-juizados"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    logging.info(f"Fetching data from {url}")
    try:
        response = requests.get(url, headers=headers, verify=False, timeout=30)
        response.raise_for_status()
        return response.text
    except Exception as e:
        logging.error(f"Failed to fetch data: {e}")
        return None

def is_valid_organ(organ_name):
    """
    Ignores purely criminal courts, but keeps them if they are both civil and criminal.
    """
    name_lower = organ_name.lower()
    is_criminal = "criminal" in name_lower or "criminais" in name_lower or "crimes" in name_lower
    is_civel = "cível" in name_lower or "civel" in name_lower
    
    if is_criminal:
        if is_civel:
            return True  # Cível e Criminal
        return False     # Apenas Criminal
    
    return True # Other organs

def process_html(html_content):
    soup = BeautifulSoup(html_content, 'html.parser')
    
    table = soup.find('table', class_=lambda c: c and 'table' in c)
    if not table:
        logging.error("Could not find the target table.")
        return []
        
    extracted_data = []
    
    tbody = table.find('tbody')
    rows = tbody.find_all('tr') if tbody else table.find_all('tr')[1:] # Skip header if no tbody
    
    logging.info(f"Found {len(rows)} rows to process.")
    
    for row in rows:
        cols = row.find_all(['td', 'th'])
        if len(cols) < 3:
            continue
            
        circunscricao = cols[0].text.strip()
        unidade_judicial = cols[1].text.strip()
        
        # Determine if it's a valid organ based on rules
        if not is_valid_organ(unidade_judicial):
            continue
            
        # Try to find email link or just extract text
        email_tag = cols[2].find('a')
        if email_tag and 'mailto:' in email_tag.get('href', ''):
            email = email_tag.get('href').replace('mailto:', '').strip()
        else:
            email = cols[2].text.strip()
            
        extracted_data.append({
            'Município': circunscricao,
            'Órgão': unidade_judicial,
            'Email': email,
            'Telefone': 'Não informado'
        })
        
    return extracted_data

def save_to_excel(data, filename="tjdft_guia_judiciario.xlsx"):
    if not data:
        logging.warning("No data to save.")
        return

    df_new = pd.DataFrame(data)
    
    if os.path.exists(filename):
        df_existing = pd.read_excel(filename)
        # Drop duplicates based on City, Organ, and Email
        df_combined = pd.concat([df_existing, df_new]).drop_duplicates(subset=['Município', 'Órgão', 'Email'], keep='last')
        logging.info(f"Updated Excel file. Total records: {len(df_combined)} (added {len(df_combined) - len(df_existing)})")
    else:
        df_combined = df_new
        logging.info(f"Created new Excel file with {len(df_combined)} records.")
        
    df_combined.to_excel(filename, index=False)
    logging.info(f"Data saved to {filename}")

if __name__ == "__main__":
    logging.info("Starting TJDFT Scraper...")
    html = fetch_data()
    if html:
        records = process_html(html)
        if records:
            save_to_excel(records)
        else:
            logging.warning("No records were extracted or all were filtered out.")
    logging.info("Scraping finished.")
