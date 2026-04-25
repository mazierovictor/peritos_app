import requests
import pandas as pd
import logging
import os

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def fetch_data():
    """
    Fetches the JSON data directly from the TJAL API endpoint.
    """
    url = "https://dadosabertos.tjal.jus.br/api/locais/todos"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    logging.info(f"Fetching data from {url}")
    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logging.error(f"Failed to fetch data: {e}")
        return None

def is_valid_organ(organ_name):
    """
    Ignores purely criminal courts, administrative units, and keeps only specific judicial units.
    """
    if not organ_name:
        return False
        
    name_lower = organ_name.lower()
    
    # 1. Filter out purely criminal
    is_criminal = "criminal" in name_lower or "criminais" in name_lower or "crimes" in name_lower
    is_civel = "cível" in name_lower or "civel" in name_lower or "civil" in name_lower
    
    if is_criminal and not is_civel:
        return False     # Apenas Criminal
        
    # 2. Blocklist of administrative and non-judicial departments
    exclusions = [
        "central de mandados", "central de penas", "centro judiciário", "centro judiciario",
        "centro de custódia", "centro de custodia", "cjus", "cejusc", "comissões", 
        "comissoes", "comissão", "comissao", "comitê", "comite", "departamento", 
        "departamentos", "direção", "direcao", "diretoria", "coordenação", "coordenadoria",
        "administração", "administracao", "almoxarifado", "arquivo", "assessoria",
        "corregedoria", "turma recursal", "ouvidoria", "presidência", "presidencia",
        "secretaria-geral", "procuradoria", "plantão", "turma de uniformização"
    ]
    for exc in exclusions:
        if exc in name_lower:
            return False

    # 3. Allowlist to strictly keep the requested types
    allowlist = ["vara", "gabinete", "única", "unica", "juizado", "câmara cível", "camara civel"]
    has_allowed = any(allowed in name_lower for allowed in allowlist)
    
    if not has_allowed:
        return False
        
    return True

def process_data(json_data):
    """
    Extracts relevant contact information from the fetched JSON structure.
    """
    if not json_data:
        logging.error("No JSON data provided.")
        return []
        
    extracted_data = []
    
    for item in json_data:
        municipio = item.get("cidade", "").strip()
        local = item.get("local", "").strip()
        unidade = item.get("unidade", "").strip()
        telefone = item.get("telefone")
        email_prefix = item.get("email")
        
        # Construct the full organ name
        if local and unidade:
            if local.lower() in unidade.lower() or unidade.lower() in local.lower():
                 orgao = f"{local}" # Avoid repetition if they are somewhat similar 
            else:
                 orgao = f"{local} - {unidade}"
        elif local:
            orgao = local
        elif unidade:
            orgao = unidade
        else:
            orgao = "Não informado"
            
        orgao = orgao.strip()
            
        # Determine if it's a valid organ based on rules (check both parts if combined)
        if not is_valid_organ(orgao):
            continue
            
        # Format Email
        email = f"{email_prefix.strip()}@tjal.jus.br" if email_prefix else "Não informado"
        
        # Format Telefone
        telefone_str = telefone.strip() if telefone else "Não informado"
            
        extracted_data.append({
            'Município': municipio,
            'Órgão': orgao,
            'Email': email,
            'Telefone': telefone_str
        })
        
    logging.info(f"Processed {len(extracted_data)} valid records out of {len(json_data)} total entries.")
    return extracted_data

def save_to_excel(data, filename="tjal_guia_judiciario.xlsx"):
    """
    Saves the extracted data to an Excel file, appending without duplicates.
    """
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
    logging.info("Starting TJAL Scraper...")
    json_data = fetch_data()
    if json_data:
        records = process_data(json_data)
        if records:
            save_to_excel(records)
        else:
            logging.warning("No records were extracted or all were filtered out.")
    logging.info("Scraping finished.")
