import requests
from bs4 import BeautifulSoup
import pandas as pd
import re
import os
import time
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def fetch_data():
    url = "https://centralservicos.tjpa.jus.br/bv/todos.php"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    logging.info(f"Fetching data from {url}")
    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        return response.text
    except Exception as e:
        logging.error(f"Failed to fetch data: {e}")
        return None

def is_valid_organ(organ_name):
    """
    Ignora tudo o que for relacionado a varas criminais, 
    exceto se for vara cível e criminal.
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
    
    # Each entry is generally inside a div that contains a strong tag for the title
    # Let's find all instances of "Comarca" strings or the structure we observed earlier
    
    # The structure we found:
    # <div class="colA">
    #   <strong>Organ Name</strong>
    #   ...text containing "Cidade : Comarca name | Tipo : Judicial..."
    #   ...text containing "E-mail : something@tjpa.jus.br | Telefone : "
    # </div>
    
    # Let's find all colA divs directly as it looked like a reliable container
    # From our structural test, there was a <div class="colA"> containing the info.
    # Alternatively we can find all <strong> tags and traverse.
    
    extracted_data = []
    
    # We saw <div class="['colA']"> in the output but it's likely class="colA"
    containers = soup.find_all('div', class_=lambda c: c and 'colA' in c)
    if not containers:
        # Fallback: look for strong tags that might be headers
        strongs = soup.find_all('strong')
        containers = [s.parent for s in strongs if s.parent.name == 'div']
        
    logging.info(f"Found {len(containers)} potential containers to process.")
    
    for container in containers:
        # Extract Organ Name
        strong_tag = container.find('strong')
        if not strong_tag:
            continue
            
        organ_name = strong_tag.text.strip()
        if not organ_name:
            continue
            
        if not is_valid_organ(organ_name):
            continue
            
        # Extract the rest of the text
        text_content = container.get_text(separator=' ', strip=True)
        
        # Regex to find Cidade
        cidade_match = re.search(r'Cidade\s*:\s*(.*?)\s*\|', text_content)
        cidade = cidade_match.group(1).strip() if cidade_match else "Não informada"
        
        # Regex to find Email
        email_match = re.search(r'E-mail\s*:\s*([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', text_content)
        
        if not email_match:
            # Fallback to general email regex in the text
            fallback_match = re.search(r'([a-zA-Z0-9._%+-]+@tjpa\.jus\.br)', text_content)
            email = fallback_match.group(1).strip() if fallback_match else "Não informado"
        else:
            email = email_match.group(1).strip()
            
        # Regex to find Phone
        phone_match = re.search(r'Telefone\s*:\s*(.*?)(?=\s*\||\s*Consulte|\s*Balcão|$)', text_content)
        phone = phone_match.group(1).strip() if phone_match else "Não informado"
        # Cleanup phone if it caught trailing words
        if "Consulte" in phone:
            phone = phone.replace("Consulte", "").strip()
            
        extracted_data.append({
            'Município': cidade,
            'Órgão': organ_name,
            'Email': email,
            'Telefone': phone
        })
        
    return extracted_data

def save_to_excel(data, filename="tjpa_guia_judiciario.xlsx"):
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
    logging.info("Starting TJPA Scraper...")
    html = fetch_data()
    if html:
        records = process_html(html)
        if records:
            save_to_excel(records)
        else:
            logging.warning("No records were extracted.")
    logging.info("Scraping finished.")
