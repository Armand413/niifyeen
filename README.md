# NiifYéén

Prototype Python de supervision vidéo multi-caméras, avec une interface web en français, détection de personnes, suivi local, événements en direct et historique SQLite.

Le dépôt contient également des fonctions de reconnaissance faciale et de recherche par photo. Il ne s’agit donc pas d’un système exclusivement anonyme. Ce document décrit l’état actuel du code ; les instructions de démarrage concernent le serveur local et la consultation des flux, sans guide de configuration de l’identification biométrique.

## Sommaire

- [Prérequis](#prérequis)
- [Lancement rapide](#lancement-rapide)
- [Installation sur une autre machine](#installation-sur-une-autre-machine)
- [Interface](#interface)
- [Configuration](#configuration)
- [Architecture](#architecture)
- [Données](#données)
- [API](#api)
- [Tests](#tests)
- [Dépannage](#dépannage)
- [Limites](#limites)

## Prérequis

- Windows est l’environnement de développement actuel. Certains chemins et outils de capture utilisent des composants propres à Windows.
- Python **3.10 minimum** pour la syntaxe employée. L’environnement local inspecté utilise Python **3.10.20**.
- Un navigateur récent.
- Une webcam ou des sources vidéo réseau accessibles pour tester les flux réels.
- Les bibliothèques Python et les modèles attendus par le code.

Le dépôt ne fournit pas de minimum matériel chiffré. La charge dépend notamment du nombre de caméras, des modèles chargés et du processeur. La configuration locale de PyTorch est une version CPU ; aucun GPU n’est requis par le code de lancement.

## Lancement rapide

Les commandes suivantes s’exécutent dans **PowerShell**, depuis la racine du projet.

### 1. Ouvrir le dossier

```powershell
Set-Location D:\HackEPO\niifyeen
```

Sur une autre machine, remplacer ce chemin par celui du projet.

### 2. Vérifier l’environnement existant

```powershell
.\venv\Scripts\python.exe --version
.\venv\Scripts\python.exe -m pip check
```

L’appel direct à l’interpréteur de `venv` évite d’avoir à activer l’environnement et ne demande pas de modifier la politique d’exécution PowerShell.

### 3. Démarrer le serveur

```powershell
.\venv\Scripts\python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

**Le démarrage ouvre les caméras configurées.** Le processus crée aussi les tables manquantes et démarre l’enregistrement des observations dans SQLite.

Conserver le terminal ouvert, puis consulter :

| Adresse locale | Contenu |
| --- | --- |
| <http://127.0.0.1:8000/> | Accueil actuel |
| <http://127.0.0.1:8000/cameras> | Flux vidéo, carte et événements |
| <http://127.0.0.1:8000/docs> | Documentation interactive des routes HTTP |
| <http://127.0.0.1:8000/redoc> | Autre présentation de la documentation HTTP |

Le WebSocket n’est pas décrit dans la documentation OpenAPI générée.

### 4. Arrêter ou redémarrer

- Arrêter avec **Ctrl + C** dans le terminal du serveur.
- Après une modification Python, relancer la commande de démarrage.
- Après une modification CSS ou HTML, recharger le navigateur avec **Ctrl + F5**.

Garder un seul processus serveur pour les essais : plusieurs workers ouvriraient chacun les caméras et disposeraient d’états mémoire indépendants. Le rechargement automatique n’est pas activé dans la commande proposée afin d’éviter des réouvertures répétées des périphériques.

## Installation sur une autre machine

Le dossier `venv` est local à une machine et ne doit pas être copié comme méthode d’installation. Il n’existe actuellement ni `requirements.txt`, ni `pyproject.toml`, ni verrouillage des dépendances dans le dépôt.

Pour créer un environnement neuf si `venv` n’existe pas :

```powershell
py -3.10 -m venv venv
.\venv\Scripts\python.exe -m pip install --upgrade pip
```

Les bibliothèques nécessaires à l’application comprennent :

| Bibliothèque | Rôle |
| --- | --- |
| FastAPI, Uvicorn | Application HTTP et serveur ASGI |
| websockets | Prise en charge WebSocket du serveur |
| python-multipart | Lecture des formulaires contenant des fichiers |
| NumPy, OpenCV | Images, tableaux numériques et capture vidéo |
| Ultralytics, PyTorch, torchvision | Détection et suivi de personnes |
| SQLAlchemy | Accès à SQLite |
| InsightFace, ONNX Runtime | Modules de vision présents dans le dépôt |

Pour transmettre la liste des versions d’un environnement fonctionnel, l’équipe peut l’exporter **depuis cet environnement** :

```powershell
.\venv\Scripts\python.exe -m pip freeze > requirements-local.txt
```

Puis, après vérification de ce fichier pour la machine destinataire :

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements-local.txt
.\venv\Scripts\python.exe -m pip check
```

Ce fichier n’est pas fourni actuellement. Un export peut contenir des chemins locaux ou des variantes CPU/GPU non portables. Une installation sur un environnement vierge n’a pas été validée ; les dépendances natives peuvent nécessiter des composants de compilation ou des paquets adaptés à la plateforme.

### Fichiers nécessaires

- `yolov8n.pt` : poids du détecteur, présents à la racine du dépôt.
- `models_insightface/` : répertoire local des modèles utilisés par les modules existants, ignoré par Git ; un clone ne le récupère pas.
- `niifyeen.db` : base locale, actuellement suivie par Git. Une base absente est créée au démarrage, mais les données existantes ne sont pas recréées.

Des bibliothèques peuvent tenter des téléchargements à la première utilisation si des ressources manquent. Un démarrage entièrement hors ligne suppose que les dépendances et modèles soient déjà disponibles. Les conditions d’utilisation des bibliothèques et des poids restent à vérifier séparément ; aucune licence de projet n’est fournie dans le dépôt.

## Interface

### Accueil

`backend/app/static/index.html` contient les formulaires existants d’enrôlement et de recherche par photo, ainsi qu’un lien vers les caméras. Les réponses des formulaires sont actuellement affichées sous forme de JSON.

### Page caméras

`backend/app/static/cameras.html` présente :

- Une mosaïque de flux vidéo MJPEG.
- L’état de connexion WebSocket du navigateur au serveur.
- Une carte schématique des points caméras.
- Un tableau d’événements en direct et un historique de parcours en mémoire.
- Des notifications visuelles et un signal sonore, que le navigateur peut bloquer tant que l’utilisateur n’a pas interagi avec la page.

L’état « WS connecté » confirme la connexion au serveur, **pas** le bon fonctionnement de chaque caméra. Le tableau de parcours affiché n’est pas la liste complète des détections stockées en base.

`layout.css` apporte la mise en page commune, l’adaptation aux petits écrans et les zones de tableaux défilantes. Il n’y a ni framework frontend ni étape de compilation JavaScript.

## Configuration

La configuration est définie directement dans les fichiers Python ; aucun chargement de fichier `.env` n’est implémenté.

| Élément | Emplacement |
| --- | --- |
| Sources et identifiants des caméras | `CAMERAS` dans `backend/app/main.py` |
| Points et libellés de la carte | `CAMERA_META` dans `backend/app/main.py` |
| Adresse de la base | `DB_URL` dans `backend/app/db.py` |
| Traitement et dimensions des images | `backend/app/person_pipeline.py` et son instanciation dans `main.py` |
| Diffusion MJPEG | `backend/app/video_stream.py` |
| Présentation web | `backend/app/static/` |

Les sources actuelles sont une webcam d’index `0` et deux URL vidéo privées du réseau local. Ces URL ne sont pas des services accessibles publiquement et ne fonctionneront pas nécessairement sur un autre réseau.

Les chemins de la base SQLite et des modèles sont relatifs au **répertoire de lancement**. Toujours lancer le serveur depuis la racine : une autre position peut créer une nouvelle base au mauvais endroit ou provoquer des recherches/téléchargements de modèles inattendus.

### Incohérence connue de la carte

Les caméras déclarées portent les identifiants `entre`, `salle`, `sortie`, alors que la carte utilise `cam0`, `cam1`, `cam2`. La carte ne peut donc pas associer correctement les événements actuels à ses points. Cette incohérence est documentée ici et n’a pas été corrigée par la rédaction du README.

## Architecture

```text
niifyeen/
├── README.md
├── .gitignore
├── yolov8n.pt
├── niifyeen.db
├── models_insightface/          # Ressources locales ignorées par Git
├── venv/                       # Environnement Python local
├── backend/app/
│   ├── main.py                 # Routes, démarrage, événements, journalisation
│   ├── person_pipeline.py      # Circuit actif : capture et analyse par caméra
│   ├── video_stream.py         # Envoi asynchrone des images MJPEG
│   ├── vision.py               # Utilitaires de vision existants
│   ├── reid.py                 # Module de rapprochement inter-caméras existant
│   ├── db.py                   # Modèles SQLAlchemy et sessions SQLite
│   ├── camera.py               # Ancien circuit de capture
│   ├── tracker.py              # Suivi simple utilisé par l’ancien circuit
│   ├── list_cams.py            # Utilitaire Windows de sondage des webcams
│   └── static/
│       ├── index.html
│       ├── cameras.html
│       └── layout.css
└── tests/
    └── test_video_stream.py
```

### Circulation des données

1. FastAPI démarre un `PersonPipeline` par caméra, dans un thread distinct.
2. OpenCV lit les images ; les modèles existants produisent les événements et les annotations.
3. Le circuit conserve la dernière image JPEG et les derniers événements en mémoire.
4. Les routes vidéo transmettent les images au navigateur ; le WebSocket transmet les événements environ toutes les 100 ms.
5. Un thread séparé écrit les observations dans SQLite, avec un espacement nominal de 0,8 seconde par caméra, piste et mode.

La diffusion vidéo n’envoie plus la même instance d’image en boucle. Elle vérifie les nouvelles images à une cadence maximale nominale de 20 images/s par connexion, avec attente asynchrone. Cela limite le trafic inutile mais ne garantit pas 20 images/s de traitement ni une latence donnée.

## Données

La base est ouverte via `sqlite:///./niifyeen.db`.

| Table | Contenu |
| --- | --- |
| `participants` | Nom, informations et date de consentement enregistrée |
| `embeddings_reference` | Représentations numériques de référence et nom du fichier source |
| `detections` | Caméra, date, piste, mode, libellé, scores et coordonnées |

Les modes existants sont `TARGET`, `DB` et `UNK`. Un identifiant de piste appartient à une caméra ; les identifiants globaux du module existant sont temporaires et en mémoire. Aucun de ces champs ne garantit une identité exacte.

Points importants :

- Les observations sont persistées, pas les vidéos complètes.
- Les coordonnées `x` et `y` sont normalisées dans l’image, pas des coordonnées géographiques.
- Les dates des détections sont produites en UTC, mais l’API actuelle les sérialise sans suffixe de fuseau horaire.
- Le parcours courant et les états de suivi en mémoire sont perdus au redémarrage.
- Le parcours est mis à jour dans la boucle WebSocket : il dépend de la présence d’un client connecté.
- La date de consentement est une valeur automatique en base ; il n’existe pas de procédure complète de recueil, retrait et suppression dans l’interface.
- Il n’y a ni purge automatique, ni migration de schéma, ni gestion complète de suppression des participants.

### Sauvegarde

Arrêter le serveur, puis sauvegarder `niifyeen.db` dans un emplacement protégé. Si des fichiers SQLite annexes existent, les préserver également avec la sauvegarde. Ne pas supprimer la base pour résoudre une erreur de schéma sans sauvegarde préalable.

`create_all()` crée les tables manquantes mais ne met pas à jour les colonnes d’une table existante. Une ancienne base peut donc nécessiter une migration explicite.

## API

### Consultation et transport

| Méthode | Route | Description |
| --- | --- | --- |
| GET | `/` | Page d’accueil |
| GET | `/cameras` | Tableau de bord vidéo |
| GET | `/api/cameras` | Liste des identifiants et métadonnées de carte |
| GET | `/video/{cam_id}` | Flux MJPEG ; 404 si caméra inconnue |
| GET | `/detections/recent` | Observations récentes en JSON |
| WebSocket | `/ws/live` | Événements et états en direct |

`/detections/recent` accepte `minutes` entre 1 et 240, avec une valeur par défaut de 10. Le résultat contient au maximum 800 observations, des plus récentes aux plus anciennes. Il n’y a pas encore de pagination ni de filtre par caméra sur cette route.

Exemple de lecture de l’historique lorsque le serveur est démarré :

```powershell
Invoke-RestMethod 'http://127.0.0.1:8000/detections/recent?minutes=10'
```

### Autres routes présentes

Le code expose également `POST /participants/enroll`, `POST /search/by-image` et `POST /target/clear`. Elles appartiennent au module biométrique existant ; ce README en inventorie la présence sans fournir de procédure d’identification par photo.

## Tests

Depuis la racine :

```powershell
.\venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

Les trois tests actuels couvrent uniquement la diffusion MJPEG :

- Absence de renvoi répété d’une image inchangée.
- Annulation d’un flux en attente.
- Fin du flux lorsque la caméra est arrêtée.

Ils utilisent des objets simulés et n’ouvrent pas les caméras, ne chargent pas les modèles et ne modifient pas la base. Ils ne valident ni la précision de détection, ni la reconnaissance, ni le fonctionnement complet de l’interface sur les caméras du site.

## Dépannage

| Symptôme | Vérification ou action |
| --- | --- |
| `No module named uvicorn` ou autre dépendance | Utiliser l’interpréteur du bon environnement et vérifier ses dépendances ; éviter de mélanger Python global et `venv`. |
| Le Python de `venv` ne démarre plus | L’environnement peut dépendre d’une installation Python déplacée. Recréer un environnement à partir d’une liste de dépendances validée. |
| Port 8000 occupé | Arrêter l’ancien serveur ou utiliser `--port 8001`, puis ouvrir l’adresse correspondante. |
| Webcam occupée / pas de signal | Fermer les autres applications utilisant la caméra, vérifier les permissions Windows et la connexion physique. |
| Caméra réseau inaccessible | Vérifier le réseau local et l’accessibilité de sa source vidéo depuis la machine serveur. |
| Carte incohérente | Voir l’incohérence des identifiants documentée plus haut. |
| Interface toujours ancienne | Effectuer un rechargement complet avec Ctrl + F5. |
| Pas de signal sonore | Interagir avec la page et vérifier le volume ainsi que les restrictions audio du navigateur. |
| Erreur de colonne SQLite | Arrêter le serveur, sauvegarder la base et examiner la nécessité d’une migration. |
| Historique vide malgré des événements | Consulter l’état de la base ; le journaliseur actuel masque certaines exceptions, donc une écriture peut échouer sans message visible. |
| Chargement initial lent | Les modèles sont chargés à la demande ou au démarrage ; des ressources manquantes peuvent déclencher des téléchargements. |
| Images lentes malgré la connexion | Distinguer le temps d’analyse, le transport réseau et le rendu du navigateur ; la limite d’envoi MJPEG ne mesure pas le débit réel du détecteur. |

## Limites

Le projet reste un prototype local :

- Pas d’authentification ni de rôles ; le démarrage proposé écoute uniquement sur `127.0.0.1`.
- Pas de gestionnaire de caméras dans l’interface.
- Pas de garantie de reconnexion après une coupure survenue pendant la lecture d’un flux ; des événements anciens peuvent rester en mémoire.
- Les tâches d’analyse et les accès à la base peuvent ralentir le serveur selon la machine.
- Les représentations faciales et certains états sont partagés par le processus ; le système n’est pas conçu pour plusieurs sessions de recherche indépendantes.
- Pas de tests d’intégration complets, de mesures de précision ou de benchmark multi-caméras documentés.
- Pas de mécanisme de migration, de conservation limitée des données ou de journal d’audit complet.
- La base et certains caches Python sont actuellement suivis par Git ; le fichier `.gitignore` ne protège pas tous les fichiers locaux.

Les flux et la base peuvent contenir des données personnelles. L’accès, la sauvegarde et la durée de conservation doivent être définis avant tout usage réel. Une exposition publique n’est pas couverte par la configuration actuelle.

## État de cette documentation

Rédigée à partir des sources et des métadonnées de l’environnement local le **20 octobre 2026**. La rédaction ne change ni les fonctions de l’application, ni les sources vidéo, ni les données. La procédure d’installation sur une machine vierge et le fonctionnement matériel des caméras restent à valider séparément.


## Plan illustratif du site (30 octobre 2026)

La zone située sous les caméras affiche désormais `backend/app/static/site-plan.svg`, un dessin original local : accueil, salle polyvalente, bureau, local technique et couloir. Le bouton « Agrandir le plan » ouvre le dessin seul. Il fonctionne sans service cartographique ni connexion externe.

Les trois repères sont des emplacements proposés, fixes. Le plan ne représente pas un relevé réel, ne comporte pas d’échelle et n’affiche ni position de personne, ni trajet, ni état de connexion. L’ancien rendu sur canvas de cette zone a été remplacé ; les métadonnées `CAMERA_META` du serveur ne pilotent pas ce dessin. La remarque précédente sur leurs identifiants reste un constat du code serveur, mais ne concerne plus le nouveau plan statique.
