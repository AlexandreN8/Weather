#!/usr/bin/env python3
import asyncio
import httpx
import csv
import io
import json
import logging
import yaml
import os
import time
from datetime import datetime, timedelta
from kafka import KafkaProducer
from apscheduler.schedulers.blocking import BlockingScheduler
from pytz import timezone

# Collect certified data from yesterday's weather stations and publish it to Kafka
# API constraint : 50 requests per minute
# Order and Command => 1 station need at least 2 requests
# Request are shared between Meteo France APIs
# Job to take over control of the API every day at 12:30 => Observation is locked during the process

# -------------------------------------------------------------------
# CONFIGURATION AND LOGGING
# -------------------------------------------------------------------
CONFIG_FILE = "config/config.yaml"
BATCH_FILE = "/app/utils/batches_corse.json"
LOG_FILE = "logs/climatological_batch_processing.log"

with open(CONFIG_FILE, "r") as file:
    config = yaml.safe_load(file)

API_BASE_URL = config["api_url"]
API_TOKEN = os.getenv("API_TOKEN")
FETCH_TIMEOUT = config["fetch_timeout"]

# In order to not exceed 50/min, we make ~1 request every 2.5 s
DELAY_BETWEEN_REQUESTS = 2.5
MAX_CONCURRENT_REQUESTS = 1

KAFKA_BROKER = config["kafka_broker"]
TOPIC_NAME = "weather-verified"
STATUS_TOPIC = "climatologique-status"  # Shared lock topic for Observation and Climatologique

producer = KafkaProducer(
    bootstrap_servers=KAFKA_BROKER,
    value_serializer=lambda v: json.dumps(v).encode("utf-8"),
    key_serializer=lambda k: str(k).encode("utf-8")
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8")
    ]
)

# -------------------------------------------------------------------
# UTILITY FUNCTIONS
# -------------------------------------------------------------------
def get_dynamic_dates_for_yesterday():
    ''' Retrieve the start and end dates for yesterday. 
    Different times are used depending on the current time. (API constraint) '''
    now_utc = datetime.utcnow()
    yesterday_utc = now_utc - timedelta(days=1)
    if (now_utc.hour < 11) or (now_utc.hour == 11 and now_utc.minute < 30):
        start_dt = yesterday_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        end_dt   = yesterday_utc.replace(hour=5, minute=59, second=59, microsecond=0)
    else:
        start_dt = yesterday_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        end_dt   = yesterday_utc.replace(hour=23, minute=59, second=59, microsecond=0)
    return (start_dt.isoformat(timespec="seconds") + "Z",
            end_dt.isoformat(timespec="seconds") + "Z")

def csv_to_dicts(csv_text, delimiter=";"):
    """
    Transform a CSV text into a list of dictionaries.
    The first row is used as headers.
    """
    f = io.StringIO(csv_text)
    reader = csv.reader(f, delimiter=delimiter)
    # Extract headers
    headers = next(reader, None)
    if not headers:
        return []

    def parse_cell(cell):
        cell = cell.strip()
        if cell == "":
            return None
        # Replace comma by dot for floats
        cell = cell.replace(",", ".")
        try:
            return float(cell)
        except ValueError:
            return cell # keep as string

    dicts = []
    for row in reader:
        # Create a dictionary for each row
        row_dict = {}
        for h, c in zip(headers, row):
            row_dict[h] = parse_cell(c)
        dicts.append(row_dict)
    return dicts

async def create_command_for_station(station_id, start_date, end_date):
    """
    /commande-station/horaire => 202=commande accepted, 429=quota, 400=error, 500=error
    """
    url = f"{API_BASE_URL}/commande-station/horaire"
    headers = {"accept": "application/json", "apikey": API_TOKEN}
    params = {"id-station": station_id, "date-deb-periode": start_date, "date-fin-periode": end_date}
    
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT) as client:
        for attempt in range(1, 4): # 3 attempts max
            try:
                resp = await client.get(url, headers=headers, params=params)
                await asyncio.sleep(DELAY_BETWEEN_REQUESTS)
                
                if resp.status_code == 202:
                    # Commande acceptée => ID de commande
                    try:
                        cmd_id = resp.json()["elaboreProduitAvecDemandeResponse"]["return"]
                        logging.info(f"[Station {station_id}] Commande=202 => ID={cmd_id}")
                        return (cmd_id, True)
                    except KeyError:
                        logging.error(f"[Station {station_id}] 202 mais pas d'ID ! {resp.text}")
                        return (None, False)

                elif resp.status_code == 429:
                    logging.warning(f"[Station {station_id}] 429 => Quota dépassé. Attente 90s...")
                    await asyncio.sleep(90) # backoff
                    continue # retry

                elif resp.status_code in [400, 401, 404, 500]:
                    logging.error(f"[Station {station_id}] Erreur {resp.status_code} => {resp.text}")
                    return (None, False)

                else:
                    logging.error(f"[Station {station_id}] Code inattendu={resp.status_code}. {resp.text}")
                    return (None, False)

            except httpx.RequestError as exc:
                logging.error(f"[Station {station_id}] Erreur réseau: {exc}")
                await asyncio.sleep(5 * attempt)
    # final failure            
    return (None, False)

async def fetch_file_for_station(command_id):
    """
    /commande/fichier => 201=CSV ready, 204=not ready yet, 404=not found, 429=quota, 401=error, 410=error, 500=error, 507=error
    return dictionary list or None
    """
    url = f"{API_BASE_URL}/commande/fichier"
    headers = {"accept": "*/*", "apikey": API_TOKEN}
    params = {"id-cmde": command_id}
    
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT) as client:
        for attempt in range(1, 4):
            try:
                resp = await client.get(url, headers=headers, params=params)
                await asyncio.sleep(DELAY_BETWEEN_REQUESTS)
                
                if resp.status_code == 201:
                    # CSV File ready
                    text_csv = resp.text
                    # Convert CSV to list of dictionaries
                    rows_as_dict = csv_to_dicts(text_csv, delimiter=";")
                    logging.info(f"[Commande {command_id}] CSV => {len(rows_as_dict)} lignes (dicos).")
                    return (rows_as_dict, "done")

                elif resp.status_code in [204, 404]:
                    # Not ready yet => pending
                    logging.info(f"[Commande {command_id}] {resp.status_code} => en cours / non dispo. Retenter plus tard.")
                    return (None, "pending")

                elif resp.status_code == 429:
                    logging.warning(f"[Commande {command_id}] 429 => Quota. Attente 90s..")
                    await asyncio.sleep(90)
                    continue # retry

                elif resp.status_code in [401, 410, 500, 507]:
                    logging.error(f"[Commande {command_id}] Erreur {resp.status_code} => {resp.text}")
                    return (None, "error")

                else:
                    logging.error(f"[Commande {command_id}] Inattendu={resp.status_code}. {resp.text}")
                    return (None, "error")

            except httpx.RequestError as exc:
                logging.error(f"[Commande {command_id}] Erreur réseau => {exc}")
                await asyncio.sleep(5 * attempt)

    return (None, "error")

async def process_batch(batch, start_date, end_date, batch_index):
    '''  Co-routine to process a batch of stations concurrently. '''
    logging.info(f"=== Début du traitement batch {batch_index+1} ===")
    
    station_cmd_ids = {}
    sem = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    async def create_one_station(station):
        s_id = station["station_id"]
        await asyncio.sleep(DELAY_BETWEEN_REQUESTS)
        cmd_id, ok = await create_command_for_station(s_id, start_date, end_date)
        if ok and cmd_id:
            station_cmd_ids[s_id] = cmd_id
        else:
            logging.info(f"[Station {s_id}] Échec => pas de récupération.")
    
    # 1) Creation of commands for each station  
    tasks = [asyncio.create_task(create_one_station(st)) for st in batch]
    await asyncio.gather(*tasks)
    
    # 2) Multiple passes to fetch files for each station
    max_passes = 5
    for pass_index in range(1, max_passes+1):
        logging.info(f"=== Passe récupération n°{pass_index}/{max_passes} ===")
        tasks = []
        
        for s_id, c_id in station_cmd_ids.items():
            if c_id is None:
                continue # already done or failed

            async def do_fetch(station_id=s_id, cmd_id=c_id):
                await asyncio.sleep(DELAY_BETWEEN_REQUESTS)
                dict_data, status = await fetch_file_for_station(cmd_id)
                if status == "done":
                    # Enrich data with station_id
                    enriched = {"station_id": station_id, "rows": dict_data}
                    producer.send(TOPIC_NAME, key=station_id, value=enriched)
                    logging.info(f"[Station {station_id}] Données publiées => commande={cmd_id}")
                    station_cmd_ids[station_id] = None

                elif status == "pending":
                    pass  # Retry next passage
                else:
                    station_cmd_ids[station_id] = None # Error => abandon

            tasks.append(asyncio.create_task(do_fetch()))

        if not tasks:
            break # No more pending

        await asyncio.gather(*tasks)
        # Cleaning
        station_cmd_ids = {sid: cid for sid, cid in station_cmd_ids.items() if cid is not None}

        if station_cmd_ids:
            logging.info(f"Il reste {len(station_cmd_ids)} commandes pending => on attend 60s.")
            await asyncio.sleep(60)
        else:
            break

    logging.info(f"=== Fin du traitement batch {batch_index+1} ===")

def notify_observation_busy():
    """ 
        Publish a 'busy' message on STATUS_TOPIC to signal that Climatologique takes control.
    """
    message = {
        "status": "busy",
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }
    producer.send(STATUS_TOPIC, value=message)
    producer.flush()
    logging.info("Signal 'busy' publié sur Kafka (topic '%s').", STATUS_TOPIC)
    
def notify_observation_resume():
    """ 
        Publish a 'free' message on STATUS_TOPIC to signal that Climatologique has finished.
    """
    message = {
        "status": "free",
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }
    producer.send(STATUS_TOPIC, value=message)
    producer.flush()
    logging.info("Signal 'free' publié sur Kafka (topic '%s').", STATUS_TOPIC)

# -------------------------------------------------------------------
# MAIN
# -------------------------------------------------------------------
def main():
    logging.info("=== Début du cycle de Climatologique ===")
    # Signal that Climatologique takes control
    notify_observation_busy()
    # Wait 60sec to refresh quotas before starting the processing
    logging.info("Attente de 60 secondes pour refresh des quotas avant de démarrer le traitement...")
    time.sleep(60)
    
    try:
        with open(BATCH_FILE, "r", encoding="utf-8") as f:
            batches = json.load(f)
    except Exception as e:
        logging.error("Erreur lors du chargement du fichier JSON: %s", e)
        return

    if not batches:
        logging.error("Aucun batch trouvé dans le fichier JSON.")
        return

    start_date, end_date = get_dynamic_dates_for_yesterday()
    logging.info(f"Lancement du traitement pour {len(batches)} batches: {start_date} => {end_date}")
    for i, batch in enumerate(batches):
        asyncio.run(process_batch(batch, start_date, end_date, i))
        if i < len(batches) - 1:
            logging.info("Pause de 60 secondes avant le batch %d.", i + 2)
            asyncio.run(asyncio.sleep(60))
    logging.info("Fin de tous les batches de ce cycle.")
    # Refresh de quotas before ending
    logging.info("Attente de 60 secondes pour refresh les quotas avant de terminer...")
    time.sleep(60)
    # Publish "free" status message to release the lock
    notify_observation_resume()

if __name__ == "__main__":
    # Setup the timezone for our cron job
    scheduler = BlockingScheduler(timezone=timezone("Europe/Paris"))
    def scheduled_job():
        logging.info("Début du job planifié (Climatologique).")
        main()
        logging.info("Fin du job planifié (Climatologique).")

    # DEV env : Run every 5 minutes
    # scheduler.add_job(scheduled_job, 'interval', minutes=5)
    # logging.info("Job planifié pour s'exécuter toutes les minutes (mode développement).")

    # PROD env : Run daily at 12:30
    scheduler.add_job(scheduled_job, 'cron', hour=12, minute=30)
    logging.info("Job planifié pour s'exécuter quotidiennement à 12h30 (mode production).")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logging.info("Arrêt du scheduler.")
