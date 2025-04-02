'''
main.py

Main orchestrator for the real-time Pump.fun token filtering pipeline.
'''

import asyncio
import os
from dotenv import load_dotenv

# Charger les variables d’environnement privées
def load_env_from_file(file_path=".private/env.conf"):
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"⚠️ Fichier {file_path} introuvable")
    load_dotenv(dotenv_path=file_path, override=True)
    print(f"✅ Variables d'environnement chargées depuis {file_path}")

# Initialisation de l'environnement
load_env_from_file()

# Variables globales accessibles depuis .env ou config
SOLANA_NODE_WSS_ENDPOINT = os.environ["SOLANA_NODE_WSS_ENDPOINT"]
RPC_HTTP_ENDPOINT = os.environ["RPC_HTTP_ENDPOINT"]

from config import *
from pipeline.A_projects_watcher.watcher import watch_new_projects
from pipeline.B_projects_monitoring.monitor import monitor_project


DEBUG = False

async def main():
    project_queue = asyncio.Queue()
    monitored_data_queue = asyncio.Queue()

    # Exemple : activer un filtre par nom (optionnel)
    filters = {
        "name_contains": "pepe",
        # "creator_address": "AdresseDuCréateur"
    }

    # Lancer le watcher des nouveaux projets
    asyncio.create_task(watch_new_projects(project_queue, filters=None, debug=DEBUG))

    # Boucle pour lancer un monitor sur chaque projet détecté
    count = 0  # <- compteur
    while True:
        project = await project_queue.get()

        if DEBUG:
            count += 1

            # Ne traiter que 1 projet sur 3
            if count % 3 != 0:
                print(f"⏭️ Projet ignoré pour test : {project['name']} ({project['mint']})")
                continue

            print(f"📦 Nouveau token à surveiller : {project['name']} ({project['mint']})")

        await asyncio.sleep(0.1)

        # Lancer le monitor pour ce token
        asyncio.create_task(
            monitor_project(project, out_queue=monitored_data_queue,debug=DEBUG)
        )

        # await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
