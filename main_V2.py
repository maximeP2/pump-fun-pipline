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

from pipeline.dispatcher import ProjectDispatcher
from pipeline.rpc_listener import rpc_listener
from pipeline.A_projects_watcher.watcher_v2 import watch_new_projects
from pipeline.B_projects_monitoring.monirot_v2 import monitor_project  # Ton fichier canvas actuel
from pipeline.B_projects_monitoring.bonding_curve_fetcher import bonding_curve_fetcher

DEBUG = True  # Active les logs



async def main():

    dispatcher = ProjectDispatcher()
    already_launched = set()  # Pour éviter de lancer deux fois le même projet

    filters = {
        "name_contains": "pepe",  # Exemple
        # "creator_address": "AdresseWallet"
    }

    # Lancer les composants asynchrones
    asyncio.create_task(rpc_listener(dispatcher, debug=DEBUG))
    asyncio.create_task(watch_new_projects(dispatcher, filters=None, debug=DEBUG))

    first_project = True
    # Boucle principale pour surveiller et lancer les monitors
    while True:
        await asyncio.sleep(0.2)

        for mint in dispatcher.monitored_projects:
            
            if first_project:
                asyncio.create_task(bonding_curve_fetcher(dispatcher, debug=DEBUG))
                first_project = False

            if mint not in already_launched:
                project = dispatcher.project_definitions[mint]
                asyncio.create_task(monitor_project(project, dispatcher, debug=DEBUG))
                # Lancer la tâche de récupération des bonding curves
                already_launched.add(mint)
                if DEBUG:
                    print(f"🚀 Monitoring started for {project['name']} ({mint})")


if __name__ == "__main__":
    asyncio.run(main())
